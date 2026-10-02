"""The worker spec: a mirror of the default agent, and the gate that keeps it current.

``kirocrew-worker`` is the default agent as it stands ON DISK plus the work-ledger
server, minus cron scheduling and any opt-in set nobody assigned it.
:func:`_write_worker_spec` derives it inside one critical section holding both files'
writer locks, and records the generation it mirrored. :func:`require_fresh_derived_spec`
re-checks that generation on every worker spawn, re-derives a stale mirror, and refuses
the spawn when it cannot -- a stale mirror would run grants the default agent does not
have.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, NamedTuple

from kiro_crew import agent as agent_mod
from kiro_crew import agent_state
from kiro_crew.agent_files import (
    AGENT_FILENAME,
)
from kiro_crew.agent_files import (
    DASHBOARD_AUTHOR_AGENT_FILENAME as _DASHBOARD_AUTHOR_AGENT_FILENAME,
)
from kiro_crew.agent_files import WORKER_AGENT_FILENAME as _WORKER_AGENT_FILENAME
from kiro_crew.agent_materialization import auto_approve, managed_mcp
from kiro_crew.agent_spec_format import parse_markdown_spec, volatile_env_keys

#: The keys the worker spec MIRRORS from the resolved default agent spec, so its
#: superset claim holds against the agent the user actually runs rather than
#: against the template that agent was assembled from. ``permissions`` is
#: deliberately absent: it is DERIVED from the mirrored ``allowedTools``, so
#: copying it would restate a value the derive already reproduces — and would
#: restate it out of a file the governance ceiling never filtered on the way in.
#: Each mirrored key and the TYPE it must have to be mirrored at all. A spec is a
#: user-writable, hand-editable JSON file, so a key can hold anything -- and every
#: pass downstream of the mirror guards with ``isinstance`` and SKIPS what it does not
#: recognise, which is silent and fails OPEN: a ``mcpServers`` holding a JSON array
#: would reach the worker with no server dropped, no ``autoApprove`` stripped and no
#: KAS rule derived. Validating at the boundary instead means a malformed value is
#: never mirrored, so the template's own (valid) value stands and those guards become
#: belt-and-braces rather than the only check.
#:
#: ``excludedTools`` is here because it is a RESTRICTION, and mirroring grants without
#: it inverts the parity claim: a default that allows ``execute_bash`` in
#: ``allowedTools`` and then excludes it would hand the worker the grant alone.
#: "Superset of what the default GRANTS" must not become "superset of what the default
#: PERMITS". ``permissions`` stays absent -- it is derived from the final grant list.
_WORKER_MIRRORED_SHAPES: dict[str, type | tuple[type, ...]] = {
    "tools": list,
    "allowedTools": list,
    "excludedTools": list,
    "mcpServers": dict,
    "model": str,
}

#: The mirrored surface, in spec order. Derived from the shape map so the two cannot
#: disagree about which keys the mirror covers.
_WORKER_MIRRORED_KEYS: tuple[str, ...] = tuple(_WORKER_MIRRORED_SHAPES)


def _canonical_grant_pattern(ref: str) -> str | None:
    """An MCP grant ref as the PATTERN it matches tools with, or ``None`` if it is not one.

    ``allowedTools`` entries are globs, not names: kiro-cli matches a tool against the
    entry, so ``"@kirocrew-cron"``, ``"@kirocrew-cron/"`` and ``"@kirocrew-cron/*"``
    all match every tool on that server, and ``"@kirocrew-cron/cron_*"`` matches a
    subset nobody spelled out. Canonicalising the three whole-server spellings to one
    pattern is what lets a single predicate reason about all of them.

    ``None`` means "not an MCP server ref" -- a builtin (``"fs_read"``), a bare glob
    (``"*"``), or a ref naming no server (``"@"``, ``"@/cron_add"``). It does NOT mean
    "cannot reach an excluded verb", and reading it that way is what let a bare ``"*"``
    auto-approve ``cron_add``: an entry with no ``@`` is a glob over the WHOLE tool
    namespace, so it reaches further than any server-scoped ref, not less far. Classify
    every entry through :func:`_grant_reaches_excluded`, which answers for both shapes.
    """
    if not ref.startswith("@"):
        return None
    server, _, tool = ref[1:].partition("/")
    if not server:
        return None
    return f"@{server}/{tool or '*'}"


def _whole_server_ref(ref: str) -> str | None:
    """The server a ref grants WHOLE, or ``None`` when it matches a narrower set.

    Three spellings mean the same thing and only one of them is obvious:
    ``"@kirocrew-cron"``, ``"@kirocrew-cron/"`` and ``"@kirocrew-cron/*"``. Kept as a
    named notion because a whole-server grant is the one case the subtraction can
    NARROW (to the template's per-tool refs) rather than drop; every other pattern
    that reaches an excluded verb has no narrower form to fall back to.
    """
    pattern = _canonical_grant_pattern(ref)
    if pattern is None:
        return None
    server, _, tool = pattern[1:].partition("/")
    return server if tool == "*" else None


def _pattern_reaches_excluded(pattern: str) -> list[str]:
    """The refs in :data:`_WORKER_EXCLUDED_GRANTS` that *pattern* would match.

    THE predicate. Every earlier version of this subtraction matched a SPELLING --
    the exact ref, then the bare server, then ``/*`` -- and each round a reviewer
    found the next spelling that slipped past: a partial-verb glob
    (``"@kirocrew-cron/cron_*"``) matches ``cron_add`` while being none of those
    three. Asking instead "could this entry match any excluded ref?" is closed under
    spelling, so a form nobody has thought of is covered by construction.

    ``fnmatchcase`` in the direction that matters: the ENTRY is the pattern and the
    excluded ref is the concrete string, because the question is what the entry would
    grant, not what the exclusion looks like. Both the raw and the case-folded pair
    are tried, and matching MORE is the safe direction here -- a match only ever
    withholds a grant, never adds one -- so a spec whose ref differs in case still
    fails closed instead of relying on a case rule this module cannot verify.
    """
    reached = [ref for ref in sorted(agent_mod._WORKER_EXCLUDED_GRANTS) if _glob_hits(ref, pattern)]
    return reached


def _glob_hits(concrete: str, pattern: str) -> bool:
    """Would *pattern*, as an ``allowedTools`` entry, match the tool named *concrete*?

    One rule in one place, because two classifiers ask it: the raw pair and the
    case-folded pair, matching MORE being the safe direction here -- a match only ever
    withholds a grant, never adds one.
    """
    return fnmatchcase(concrete, pattern) or fnmatchcase(concrete.casefold(), pattern.casefold())


def _excluded_verb(ref: str) -> str:
    """The bare tool name an excluded ``@server/verb`` ref names."""
    _, _, verb = ref.partition("/")
    return verb


def _grant_reaches_excluded(entry: str) -> list[str]:
    """The excluded refs an ``allowedTools`` ENTRY would auto-approve. Answers for ALL.

    The entry point, and it classifies every entry rather than only the ``@``-prefixed
    ones. :func:`_canonical_grant_pattern` answers ``None`` for an entry that is not an
    MCP server ref, and treating that as "reaches nothing" was a fail-OPEN hole: a bare
    ``"*"`` is kiro-cli's spelling for "every tool", so it auto-approves
    ``@kirocrew-cron/cron_add`` while skipping the predicate entirely. A non-``@`` entry
    is a glob over the WHOLE namespace, which reaches further than any server-scoped
    ref, so it is matched against each excluded ref in BOTH spellings the namespace
    offers -- the full ``@server/verb`` ref and the bare verb -- and either hit counts.

    Written with a single ``return`` at the end and no early exit, on the same discipline
    :func:`_require_fresh_worker_spec` carries: every earlier version of this
    subtraction grew a shortcut for a shape it did not want to think about, and each of
    those shortcuts was a grant reaching an excluded verb unexamined. Falling off the end
    is the only exit, so every entry leaves here classified.
    """
    pattern = _canonical_grant_pattern(entry)
    reached: list[str] = []
    for ref in sorted(agent_mod._WORKER_EXCLUDED_GRANTS):
        if pattern is None:
            # Namespace-wide glob: the ENTRY is the pattern, and the excluded tool is
            # reachable under either spelling the namespace offers.
            verb = _excluded_verb(ref)
            hit = _glob_hits(ref, entry) or (verb != "" and _glob_hits(verb, entry))
        else:
            hit = _glob_hits(ref, pattern)
        if hit:
            reached.append(ref)
    return reached


def _apply_worker_exclusions(granted: list[str], *, template_grants: list[str]) -> list[str]:
    """Remove :data:`_WORKER_EXCLUDED_GRANTS` from a mirrored grant list.

    Two shapes reach here and only one of them is an exact match. A ref naming an
    excluded verb is dropped. A WHOLE-SERVER grant — ``"@kirocrew-cron"``, which is
    what the default agent carries once a rebuild has widened it — covers the
    excluded verb too, so carrying it across would grant ``cron_add`` by the back
    door while the exclusion list read as honoured.

    A whole-server grant on an excluded server is therefore replaced by the SHIPPED
    TEMPLATE's own per-tool grants for that server. Those per-tool refs are the
    worker's narrowed cron surface: the template auto-approves the reading verbs and
    names none of the excluded three, so substituting them states the narrowing in
    one place instead of enumerating a server's surface here, where a verb added to
    the server tomorrow would silently join the worker's allowlist. It also fails
    CLOSED — a template that grants nothing for that server leaves the worker
    prompting rather than auto-approved.

    The pass is applied after every source has been folded in, so a ref that arrives
    from the previous worker file is excluded on the same terms as one mirrored from
    the default. That is deliberate: this is a policy about what a worker may skip
    the gate for, not a preference the file it was written into can overrule.

    Deduplicates while it filters, so a default carrying both the whole-server grant
    and its per-tool refs yields each ref once. Every drop and every narrowing is
    reported as one ``mcp_auto_approve_withheld`` SEL record, the same event the
    ceiling and the conductor grant filter emit, because a grant the default agent
    auto-approves and the worker does not is a permission decision an operator has to
    be able to find.
    """
    kept: list[str] = []
    withheld: list[str] = []
    for ref in granted:
        # EVERY entry is classified, including a bare glob with no ``@``: skipping those
        # is how ``allowedTools: ["*"]`` auto-approved an excluded verb.
        reaches = _grant_reaches_excluded(ref)
        if not reaches:
            # Exact non-excluded refs, builtins, and globs that cannot reach an
            # excluded verb pass through untouched — the subtraction is cron
            # scheduling, not a general narrowing of what the default granted.
            if ref not in kept:
                kept.append(ref)
            continue
        whole = _whole_server_ref(ref)
        if whole is not None:
            # The one case with a narrower form to fall back to: the template's own
            # per-tool grants for that server ARE the worker's cron surface.
            substitutes = [
                sub
                for sub in template_grants
                if _canonical_grant_pattern(sub) is not None
                and _whole_server_ref(sub) is None
                and (sub.split("/", 1)[0] == f"@{whole}")
                and not _grant_reaches_excluded(sub)
            ]
            withheld.append(f"{ref} (narrowed to {', '.join(substitutes) or 'nothing'})")
            for sub in substitutes:
                if sub not in kept:
                    kept.append(sub)
            continue
        # A narrower pattern that still reaches an excluded verb has no safe subset to
        # fall back to: "every cron verb starting cron_ except cron_add" has no
        # spelling in this field. Dropped, which fails CLOSED — the tools stay
        # mounted and their calls reach the approval gate.
        withheld.append(f"{ref} (reaches {', '.join(reaches)})")
    if withheld:
        # Withholding a grant is a permission DECISION, and every other writer of an
        # ``allowedTools`` list emits this same event for it — ``_apply_allowed_tools_ceiling``
        # and ``_filter_auto_approve`` both do. Without it a worker silently starts
        # prompting for a verb the default agent auto-approves and the operator has no
        # record of which rule did it. Best-effort: the audit must never break an install.
        try:
            agent_mod.sel().log_api_access(
                caller="system",
                operation="mcp_auto_approve_withheld",
                outcome="ok",
                source="_install_worker_agent",
                resources=(
                    f"{', '.join(withheld)} not auto-approved on the worker "
                    "(a recurring job outlives the item); calls go through the approval gate"
                ),
            )
        except Exception:  # noqa: BLE001 — the audit must not break the install
            agent_mod.logger.debug("SEL audit unavailable for withheld worker grant", exc_info=True)
    return kept


def _strip_excluded_auto_approve(servers: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Remove :data:`_WORKER_EXCLUDED_GRANTS` verbs from a mirrored ``autoApprove``.

    ``autoApprove`` on an ``mcpServers`` entry is the OTHER route to the exemption
    ``allowedTools`` grants, and a more direct one: kiro-cli approves an autoApproved
    MCP tool locally and emits no permission request, so ``hooks.on_tool_call`` never
    runs for it. Filtering the grant list alone therefore leaves the subtraction with
    a back door — the same shape as a whole-server grant covering an excluded verb,
    one channel over.

    :func:`strip_ungoverned_auto_approve` does not close it. That pass is the
    governance CEILING and is whole-server: it keeps the key intact whenever
    ``may_skip_gate_now("@<server>")`` allows the server, which on an ungoverned host
    is always. So an ``autoApprove: ["cron_add"]`` mirrored off the default survives
    it. This pass is the worker POLICY, and the two are independent filters that both
    have to run.

    Per VERB rather than per key, so a default that auto-approves ``cron_list``
    alongside ``cron_add`` keeps the reading verb — the same line the grant exclusion
    draws. An entry left with nothing is dropped rather than kept empty: absent and
    empty mean the same thing to the runtime, and the shorter spec is the honest one.

    Returns the new map and what it removed, so the caller can report the decision.
    """
    out: dict[str, Any] = {}
    removed: list[str] = []
    for name, spec in servers.items():
        approved = spec.get("autoApprove") if isinstance(spec, dict) else None
        if not isinstance(approved, list):
            out[name] = spec
            continue
        # The SAME predicate, on the same canonical form: an ``autoApprove`` name is a
        # pattern over that server's verbs, so ``"cron_*"`` and ``"*"`` reach
        # ``cron_add`` exactly as the grant globs do. Nothing here is narrowed -- the
        # field holds patterns, and "cron_ anything except cron_add" has no spelling
        # in it -- so a reaching entry is dropped and the verbs it covered go through
        # the approval gate.
        kept = []
        for entry in approved:
            if not isinstance(entry, str):
                kept.append(entry)
                continue
            reaches = _pattern_reaches_excluded(f"@{name}/{entry}")
            if reaches:
                removed.append(f"{name}/{entry} (reaches {', '.join(reaches)})")
            else:
                kept.append(entry)
        if len(kept) == len(approved):
            out[name] = spec
            continue
        trimmed = dict(spec)
        if kept:
            trimmed["autoApprove"] = kept
        else:
            trimmed.pop("autoApprove", None)
        out[name] = trimmed
    return out, removed


def _worker_unassignable_servers() -> frozenset[str]:
    """Managed ``opt_in`` servers the mirror must not carry onto the worker.

    An ``opt_in`` server is an ASSIGNABLE SET, not an always-on capability: both
    spec-writing loops skip it and the agents that need one hand-build the entry,
    which IS the explicit per-agent assignment such a set requires. So a user who
    mounts one on the DEFAULT agent has assigned it to that agent — not to every
    spec derived from it, and a mirror that inherited the assignment would be
    granting a set nobody assigned.

    ``kirocrew-dashboard`` is why this is load-bearing rather than tidy. It carries
    ``session_send``, and a worker's inability to reach that tool is structural
    rather than withheld: the ledger is a worker's ONLY channel to its conductor,
    and ``send_message`` / ``send_notification`` address a *person* rather than a
    session's turn queue. Mirroring the server would make that guarantee
    conditional on what the operator happens to have mounted on their own agent.

    ``kirocrew-work`` is excluded from the exclusion because this installer assigns
    it explicitly, which is the one way an opt-in set is meant to arrive. Derived
    from the registry rather than listed, so an opt-in server added tomorrow is
    withheld by default instead of reaching the worker until someone notices.
    """
    return frozenset(
        name
        for name, spec in agent_mod._MANAGED_MCP_SERVERS.items()
        if spec.get("opt_in") and name != "kirocrew-work"
    )


def _drop_servers(config: dict, servers: frozenset[str]) -> list[str]:
    """Remove *servers* and every ref naming them from a spec, in place.

    Returns what it removed, so the caller can report the decision. Three surfaces
    because a server reaches a session through any of them: the ``mcpServers`` entry
    kiro-cli launches, the ``@server`` ref in ``tools`` that exposes its tools, and
    any grant in ``allowedTools``. ``permissions`` needs no pass — it is derived
    from ``allowedTools`` after this.
    """
    removed: list[str] = []
    mcp = config.get("mcpServers")
    if isinstance(mcp, dict):
        for name in sorted(servers):
            if mcp.pop(name, None) is not None:
                removed.append(name)
    for key in ("tools", "allowedTools"):
        refs = config.get(key)
        if not isinstance(refs, list):
            continue
        kept = []
        for ref in refs:
            if isinstance(ref, str) and ref.startswith("@"):
                server = ref[1:].split("/", 1)[0]
                if server in servers:
                    removed.append(f"{key}:{ref}")
                    continue
            kept.append(ref)
        config[key] = kept
    return removed


def _installed_default_spec() -> dict[str, Any] | None:
    """The default agent spec as it stands ON DISK, or ``None`` when unusable.

    ``build_agent_config`` composes the shipped template with the user override
    file, and that is not where a user's own additions live. An app registration,
    a merge out of a shared ``mcp.json``, a server the dashboard mounts and the
    ``config.json`` model pick all land in ``kirocrew.json`` itself, through the
    refresh path that treats ``tools``/``allowedTools`` as user-owned. So a
    derived spec claiming parity with the default agent has to read that file:
    assembled from the template alone it carries the managed servers only, and a
    dispatched worker is then missing the very tools its dispatcher holds.

    Read through the capped reader for the reason ``_install_heartbeat_agent``
    gives at the same seam: the agents directory is user-writable and
    tool-shared, so an oversized or non-JSON "spec" is refused at the gate rather
    than slurped into memory. ``None`` on a fresh install where the file does not
    exist yet, which leaves the caller on the template — the only base available
    when there is nothing to mirror.
    """
    return agent_mod._read_spec_capped(agent_mod.kiro_agents_dir_path() / AGENT_FILENAME)


def _worker_model_is_user_pinned() -> bool:
    """True when the worker's model must NOT be overwritten by the mirror.

    The model is mirrored from the default spec so a worker runs the dispatcher's
    own choice rather than the shipped sentinel, and an explicit per-agent pick
    is the one case where that is wrong. The distinction already exists and is
    recorded in the ``agent_state`` sidecar rather than inferred from the spec:
    the dashboard's model PATCH sets ``model_managed`` False on an explicit pick
    and back to True when the field is cleared, which is precisely "this value is
    mine, stop propagating into it".

    THREE states reach here, and the third is why this is not one comparison. A
    recorded ``False`` is a pin. No entry at all is propagation, not a pin — every
    worker spec written before this reads that way, and those are the stale ``auto``
    files the mirror exists to heal. A sidecar that is PRESENT but will not parse is
    neither: ownership is unknown, and the two available answers are not
    symmetric. Mirroring over a pin destroys a value that lives nowhere else (the
    sidecar records the flag, the spec records the model), while declining to mirror
    leaves a stale model the next readable refresh heals. So the read is ``strict``
    and an unreadable sidecar fails CLOSED — the same rule ``agent_state._read``
    already states for its mutators, applied here because this answer feeds a write.
    """
    try:
        return agent_state.get_model_managed("kirocrew-worker", strict=True) is False
    except (OSError, ValueError):
        agent_mod.logger.warning(
            "Agent state sidecar unreadable; keeping the worker spec's own model rather "
            "than overwriting a pin whose value is recorded nowhere else",
            exc_info=True,
        )
        return True


def _foreign_worker_spec_reason(spec: dict[str, Any] | None) -> str | None:
    """Why *spec*, read from the mirror path, is not this derivation's own, or ``None``.

    Takes the PARSE rather than the path, so a caller that must also use those bytes
    does not read the file twice: two reads are two observations, and the second could
    describe a different generation than the one that was attributed.

    PROVENANCE, not existence, and the distinction is the whole point. Existence is
    what the freshness check reasons about, and a file at this path that the
    derivation did not write is not stale against the default: replacing it destroys
    whatever put it there.

    ``None`` -- meaning "ours to write" -- covers three states, and the third is
    deliberate:

    * the file is ABSENT, so there is nothing to attribute;
    * it carries both marks every derivation writes (see below);
    * it does not parse as an agent spec object at all. A broken file at this path
      is not somebody's work to protect, and refusing to replace it would leave the
      worker permanently undispatchable on a host with one truncated write behind it.
      A bundle's spec is digest-verified before install and therefore parses, so this
      does not reach the case the refusal exists for.

    The two marks are the declared ``name`` and a reference to the ``kirocrew-work``
    server -- the mount that IS this agent, and the reason it exists at all. Both have
    been written by every release that produced a mirror, which is what keeps an
    upgrade from turning into a refusal: ``test_worker_agent.py`` pins that a mirror
    left by an older build is HEALED rather than needing a hand-edit, and a mark chosen
    from the current spec's shape (the exact prompt, the mounted server map) would
    refuse those files instead of repairing them. So the reference is accepted wherever
    a release put it -- the server map, ``tools``, or a per-tool grant.

    Neither mark is a secret and a spec could forge them; forging them volunteers the
    forger's own file to be replaced, which costs nothing. What cannot happen is the
    reverse: an ordinary shared crew -- its own prompt, its own servers, its own name
    -- being read as a mirror.
    """
    if not isinstance(spec, dict):
        return None
    declared = spec.get("name")
    if declared != Path(_WORKER_AGENT_FILENAME).stem:
        return f"it declares the agent name {declared!r}"
    servers = spec.get("mcpServers")
    mounts_work = isinstance(servers, dict) and "kirocrew-work" in servers
    # Each container is shape-checked before it is iterated, not only its elements: a
    # hand-edited spec holding ``"tools": 1`` would otherwise raise ``TypeError`` out of
    # an attribution, which is not a ``DerivedSpecStale`` and so reaches the spawn
    # callers as an unhandled error instead of a declined dispatch. The same discipline
    # ``_WORKER_MIRRORED_SHAPES`` applies to the default spec's keys. A value of the
    # wrong type carries no reference, so it contributes nothing rather than refusing on
    # its own -- the verdict stays about the marks, not about the file's tidiness.
    refs_work = any(
        ref == "@kirocrew-work" or ref.startswith("@kirocrew-work/")
        for key in ("tools", "allowedTools")
        for ref in (spec[key] if isinstance(spec.get(key), list) else ())
        if isinstance(ref, str)
    )
    if not mounts_work and not refs_work:
        return "it does not reference the kirocrew-work server that defines this agent"
    return None


def _refuse_foreign_worker_spec(path: Path, spec: dict[str, Any] | None) -> None:
    """Raise :class:`ForeignAgentSpec` when *spec*, read from *path*, is not ours.

    *path* is carried for the message and the log only; the verdict is about the bytes
    the caller already read.
    """
    reason = _foreign_worker_spec_reason(spec)
    if reason is None:
        return
    # ERROR, not debug: the boot installer's own caller swallows the exception at debug
    # level, so without this line the one event an operator needs -- "your shared crew
    # is occupying the mirror's filename" -- would be invisible at any ordinary level.
    agent_mod.logger.error(
        "Refusing to overwrite %s: %s, so it was not written by this derivation. A crew "
        "installed under this filename is served from %s instead; a hand-placed spec "
        "must be moved or renamed before the worker can be derived again.",
        path,
        reason,
        path.parent / f"crew-{path.stem}.json",
    )
    raise ForeignAgentSpec(
        f"{path} holds a spec this derivation did not write ({reason}); refusing to "
        f"overwrite it with the derived {_WORKER_AGENT_FILENAME} mirror"
    )


def _install_worker_agent() -> None:
    """Generate and install the kirocrew-worker agent config.

        The SUPERSET of the default agent, which is the whole distinction worth
        keeping: everything the default agent already grants, plus the opt-in
        ``kirocrew-work`` server, plus a prompt carrying the reporting contract. A
        NARROWED worker spec was considered and rejected — a worker writes files, runs
        builds and drives git, so anything a narrowed spec withheld would be something
        some work item needs, which is the same defect an omitted ``agent`` on
        ``session_create`` produces by handing the child ``kirocrew-conductor``
        (no ``fs_write``, cannot do the work).

        "The default agent" is the spec ON DISK, not the template it was assembled
        from — :func:`_installed_default_spec` says why that difference is the whole
        point. The keys in :data:`_WORKER_MIRRORED_KEYS` are mirrored from it and the
        work server plus its two grants are added on top, so a server the user mounts,
        a grant they add and the model they pick all reach the worker on the next
        refresh, while a tool the ceiling withholds on the default stays withheld here.

    TWO things are SUBTRACTED rather than inherited, and both exist because the
        mirror would otherwise widen a worker's reach on its own. The cron grants in
        :data:`_WORKER_EXCLUDED_GRANTS` — a recurring job outlives the item, the session
        and the dispatch, so a worker does not auto-approve authoring one (see that
        constant for why those three verbs and not the reading ones, and why the tool
        stays mounted). And the servers :func:`_worker_unassignable_servers` names — an
        ``opt_in`` set is assigned per agent, so one the operator mounted on their own
        agent is not thereby assigned to every worker they dispatch. The whole spec is
        ``default + @kirocrew-work − cron scheduling − the opt-in sets nobody assigned
        here``.

        The spec is otherwise a FUNCTION of those inputs, and the previous worker file
        contributes exactly one field to it: a ``model`` the user froze with an explicit
        pick. Carrying anything else forward was tried and removed. The worker file is
        derived, so an entry in it is either a copy of the default's or the user's own and
        nothing on disk says which — and an add-only merge therefore resurrects a server
        the default has since DROPPED (register an app, deregister it, and its tools stay
        callable on the worker for good) while also re-admitting an ``autoApprove`` no
        ceiling has seen. Preserving a user's worker-file edits is worth doing, but it
        needs a provenance record this change does not introduce.

        ``work_brief`` and ``work_report`` are auto-approved because a worker that must
        ask permission to say it is blocked will not say it, and an unattended
        dispatch is exactly the case the ledger exists for. Every grant on the
        assembled list — template, mirror, preserved or added here — passes the
        governance ceiling in ONE final filter, so a host that governs a ref gets a
        prompt rather than a bypass.
    """
    config = agent_mod.build_agent_config()
    # Captured BEFORE the mirror below overwrites it. The template's per-tool cron
    # grants ARE the worker's narrowed cron surface, and they are what a
    # whole-server grant on the default agent is replaced by — see
    # ``_apply_worker_exclusions``.
    template_grants = [ref for ref in (config.get("allowedTools") or []) if isinstance(ref, str)]
    config["name"] = "kirocrew-worker"
    config["description"] = (
        "A dispatched worker: does one work item's actual work with the full "
        "default toolset, and reports status against that item as structured "
        "data its conductor reads without interpreting a transcript."
    )
    config["prompt"] = agent_mod._WORKER_SYSTEM_PROMPT

    agents_dir = agent_mod.kiro_agents_dir_path()
    agents_dir.mkdir(parents=True, exist_ok=True)
    path = agents_dir / _WORKER_AGENT_FILENAME
    # ONE critical section from the DEFAULT read through the worker write, holding
    # both files' writer locks, because this function reads one file and writes
    # another and each has its own independent writers.
    #
    # ``agents_spec_lock`` is the template-spec lock every other read-modify-writer
    # in this module holds (the reset path, the fork refresh, the dashboard PATCH):
    # it serializes against anyone editing ``kirocrew-worker.json`` under us.
    # ``bridges._mcp_lock`` is ``kirocrew.json``'s OWN writer lock -- the one the app
    # MCP registration path takes for its read-modify-write of that file, as the
    # comment above ``_finalize_and_write`` spells out. Without it, a deregistration
    # landing after the mirror snapshot leaves a removed server's grant auto-approved
    # on the worker; ``agents_spec_lock`` alone would not serialize against it,
    # because that writer does not hold it.
    #
    # LOCK ORDER is established HERE, since nothing else in the tree nests these two:
    # the file this function WRITES outermost, the file it READS innermost. A future
    # nester takes them in that order.
    from kiro_crew.apps.bridges import _mcp_lock  # noqa: PLC0415 - boot path

    with agent_mod.agents_spec_lock(agents_dir), _mcp_lock():
        _write_worker_spec(config, path, template_grants=template_grants)
    agent_mod.logger.info("Installed worker agent config: %s", path)


def _write_worker_spec(config: dict, path: Path, *, template_grants: list[str]) -> None:
    """Mirror the default onto *config* and write it to *path*. Caller holds the locks.

    Split out so the critical section in :func:`_install_worker_agent` is one
    statement rather than a long indented block -- the transform is pure dict work on
    small maps, so holding both locks across it costs nothing and is what makes the
    mirror a SNAPSHOT rather than a read that may already be stale by the write.

    Raises :class:`ForeignAgentSpec` when *path* already holds a spec this derivation
    did not write, INSIDE the critical section: the attribution and the write it
    guards have to be one locked step, or a spec landing between them is refused on
    the previous file's provenance and overwritten anyway.
    """
    # ONE read of the existing mirror, serving both things this function needs from it:
    # whether the file is ours to replace at all, and the frozen ``model`` further down.
    # Reading it twice would be two observations of a file this critical section is about
    # to overwrite, and the attribution would then vouch for bytes other than the ones
    # the model pin came from.
    existing = agent_mod._read_spec_capped(path)
    _refuse_foreign_worker_spec(path, existing)
    # Stat BEFORE the read, so the bookkeeping below can prove the file did not move
    # while this derivation mirrored it.
    default_identity_before = default_spec_identity()
    installed_default = _installed_default_spec()
    if installed_default is not None:
        for key, shape in _WORKER_MIRRORED_SHAPES.items():
            if key not in installed_default:
                continue
            value = installed_default[key]
            if not isinstance(value, shape) or isinstance(value, bool):
                # Not mirrored, so the template's own value stands. Reported rather
                # than passed on: a hand-edited default whose key holds the wrong
                # type is a spec kiro-cli itself would reject, and silently copying it
                # would carry it past every ``isinstance`` guard downstream.
                agent_mod.logger.warning(
                    "Default agent spec key %r holds %s, not %s; not mirrored onto the "
                    "worker (the template's value stands)",
                    key,
                    type(value).__name__,
                    getattr(shape, "__name__", shape),
                )
                continue
            config[key] = copy.deepcopy(value)
        # Applied to the MIRROR itself, before this installer adds its own server:
        # an ``opt_in`` set is assigned per agent, and mounting one on the default
        # agent is not assigning it to every spec derived from that agent. See
        # ``_worker_unassignable_servers`` for why ``kirocrew-dashboard`` in
        # particular must not arrive this way.
        unassigned = _drop_servers(config, _worker_unassignable_servers())
        if unassigned:
            # Same event, same footing as every other permission decision in this
            # installer: a set the default agent holds and the worker does not is
            # something an operator has to be able to find. Never raises.
            try:
                agent_mod.sel().log_api_access(
                    caller="system",
                    operation="mcp_auto_approve_withheld",
                    outcome="ok",
                    source="_install_worker_agent",
                    resources=(
                        f"{', '.join(unassigned)} not mirrored onto the worker "
                        "(an opt-in set is assigned per agent, not inherited)"
                    ),
                )
            except Exception:  # noqa: BLE001 — the audit must not break the install
                agent_mod.logger.debug("SEL audit unavailable for unmirrored server", exc_info=True)

    tools = [ref for ref in (config.get("tools") or []) if isinstance(ref, str)]
    if "@kirocrew-work" not in tools:
        tools.append("@kirocrew-work")
    config["tools"] = tools

    granted = [ref for ref in (config.get("allowedTools") or []) if isinstance(ref, str)]
    granted.extend(ref for ref in agent_mod._WORKER_WORK_GRANTS if ref not in granted)
    config["allowedTools"] = granted

    mcp = dict(config.get("mcpServers") or {})
    # Hand-built because ``kirocrew-work`` is ``opt_in``: neither spec-writing loop
    # emits it, and this installer granting it IS the explicit per-agent
    # assignment such a set requires.
    mcp["kirocrew-work"] = managed_mcp._managed_opt_in_entry("mcp-work")
    config["mcpServers"] = mcp

    # Scheduling is subtracted LAST of the grant passes, so it applies to the whole
    # assembled list at once — the default's whole-server cron grant included.
    # ``worker = default + @kirocrew-work − cron scheduling``.
    config["allowedTools"] = _apply_worker_exclusions(
        config["allowedTools"], template_grants=template_grants
    )

    # The same subtraction, on the OTHER channel a call can skip the gate through.
    # A grant filter cannot see ``autoApprove``, and the ceiling pass below is
    # whole-server, so neither covers a mirrored ``autoApprove: ["cron_add"]``.
    config["mcpServers"], unapproved = _strip_excluded_auto_approve(config["mcpServers"])
    if unapproved:
        try:
            agent_mod.sel().log_api_access(
                caller="system",
                operation="mcp_auto_approve_withheld",
                outcome="ok",
                source="_install_worker_agent",
                resources=(
                    f"{', '.join(unapproved)} removed from a mirrored autoApprove on the "
                    "worker (a recurring job outlives the item); calls go through the gate"
                ),
            )
        except Exception:  # noqa: BLE001 — the audit must not break the install
            agent_mod.logger.debug("SEL audit unavailable for withheld autoApprove", exc_info=True)

    # ONE ceiling pass over the whole assembled list, so it covers every source at
    # once: the template's grants, the mirror of the default spec, and the two grants
    # above. Placing it after them is what keeps the ceiling authoritative.
    auto_approve._apply_allowed_tools_ceiling(config, source="_install_worker_agent")

    # The SECOND way a call skips the PreToolUse gate is ``autoApprove`` on an
    # ``mcpServers`` entry, and ``allowedTools`` filtering does not touch it. The
    # mirror copies the default's map verbatim, so a hand-added ``autoApprove`` there
    # would arrive on the worker ungoverned — the same reason
    # ``rebuild_agent_config`` runs this pass over the primary spec's map.
    config["mcpServers"] = auto_approve._strip_ungoverned_auto_approve(config["mcpServers"])

    # Derived from the FILTERED grant list rather than restated as a literal, so a
    # ceiling that strips a grant strips its KAS rule with it, and the cron
    # subtraction reaches the KAS backend rather than stopping at ``allowedTools``,
    # which nothing reads there. The shared writer version-gates it.
    auto_approve._write_derived_permissions(config, config["allowedTools"], _WORKER_AGENT_FILENAME)

    if isinstance(existing, dict) and "model" in existing and _worker_model_is_user_pinned():
        # An explicit per-agent pick outranks the mirror, and it has to be read back
        # off the file: the template the mirror falls back to carries the shipped
        # sentinel, so leaving this out would clobber the pin on a host whose default
        # spec is missing just as surely as the mirror would. It is the ONLY field
        # taken from the previous worker file — see ``_install_worker_agent`` for why
        # nothing else is.
        #
        # TYPE-CHECKED before it is carried across, on the same grounds the mirror loop
        # above checks the default's keys: the value arrives from the dashboard's model
        # PATCH and from the file itself, neither of which guarantees a string, and
        # kiro-cli validates the spec strictly — so copying a number, a list or a null
        # through would write a worker spec the agent cannot load at all, turning a
        # cosmetic bad pin into a worker that will not start. A blank string is refused
        # for the same reason it is not a pick: it names no model.
        pinned = existing["model"]
        if isinstance(pinned, str) and pinned.strip():
            config["model"] = pinned
        else:
            # The mirrored default stands, which is the recoverable direction: a worker
            # that runs the dispatcher's own model is worse than the user's pick and far
            # better than one that cannot start. The TYPE is reported and the value is
            # not -- a malformed model field is a shape problem, and the field can hold
            # anything a PATCH put there.
            agent_mod.logger.warning(
                "%s key %r holds %s, not a non-empty str; the mirrored default model "
                "stands and the pin is not carried across",
                _WORKER_AGENT_FILENAME,
                "model",
                type(pinned).__name__,
            )
    agent_mod._atomic_json_write(path, config)
    # Recorded INSIDE the critical section, against the same default-spec read this
    # derivation used: stamping it after the locks release would record a generation
    # other than the one the spec on disk mirrors.
    try:
        # ONE observation, not two. The fingerprint is of the very bytes this derivation
        # mirrored -- going back to the file for it would record a generation the spec on
        # disk does not mirror -- and the identity is recorded ONLY when a re-stat proves
        # the file held still while those bytes were being mirrored. Two independent
        # reads produce a TORN pair, an identity from one generation carrying a
        # fingerprint from another, and a later check that matched the identity would then
        # accept a mirror built from different content.
        #
        # ``_mcp_lock`` is the default spec's own writer lock, but not every writer of
        # that file takes it, so the coherence check is what makes this pair sound rather
        # than the lock.
        agent_state.set_mirrored_from(config["name"], _spec_fingerprint(installed_default))
        coherent = (
            default_identity_before is not None
            and default_spec_identity() == default_identity_before
        )
        # CLEARED rather than recorded when the file moved. No identity means no fast
        # path, so the next check compares the truthful fingerprint above against the
        # default as it then stands and re-derives on a mismatch. That direction costs one
        # re-derive; the other starts a worker on a spec nobody verified. A crash between
        # the two writes lands in the same safe place, for the same reason.
        agent_state.set_mirrored_stat(config["name"], default_identity_before if coherent else None)
    except Exception:  # noqa: BLE001 — an unwritable sidecar costs a re-derive, not the spec
        agent_mod.logger.warning("Could not record the mirrored-from bookkeeping", exc_info=True)


#: How many times :func:`require_fresh_derived_spec` re-runs its verification when the
#: default spec moves underneath it. Bounded because the loop's exit is another process
#: leaving the file alone: unbounded it would spin on a host rewriting the spec in a loop,
#: and a spawn that never returns is worse than one that refuses.
_DEFAULT_SPEC_OBSERVATION_ATTEMPTS = 3


class DerivedSpecSnapshot(NamedTuple):
    """What a freshness check VERIFIED, so a later check can prove it still holds.

    Returned by :func:`require_fresh_derived_spec` and consumed by
    :func:`require_unchanged_derived_spec`. The pair brackets a window this process
    cannot lock: kiro-cli reads the worker spec itself, in another process, some
    milliseconds after the gate passed, so a revocation landing in between is
    verified-then-changed. Holding a writer lock across that read is not available --
    the reader is a subprocess, and the lock would have to outlive this process's own
    critical section -- so the window is CLOSED BY DETECTION instead: a write that
    lands before the subprocess has read cannot escape the second check, and one that
    lands after cannot affect what it already read.
    """

    identity: str
    """The default spec's file identity, and the fingerprint below is of the bytes THAT
    stat described -- one observation, never two."""

    fingerprint: str

    spec: dict[str, Any] | None = None
    """The DERIVED spec, parsed, exactly as the gate verified it.

    Carried on the snapshot so an in-process consumer projects the bytes the gate
    verified rather than re-reading the file afterwards. A read taken after the gate
    returns is a second observation however tight the sequence looks: a revocation
    landing in between is projected as the session's whole tool surface as though it had
    been checked, and no lock closes that because both halves are this process's own
    reads. ``None`` only where the bracket does not apply.
    """


class DerivedSpecStale(RuntimeError):
    """A derived agent spec does not match the default spec and cannot be repaired.

    Raised on the SPAWN path, where the only safe answer is to refuse. A worker
    whose mirror predates a trust revocation still has the revoked server mounted
    and auto-approved, so starting it runs ungoverned grants; a refused dispatch is
    recoverable and reportable, which is the whole point of the work ledger.
    """


# Named by the path callers import it from: an error chain renders each raised
# class as ``module.qualname`` (``subagent._describe_exception``), and that text
# reaches the Subagents panel, so the owner module must not leak into it.
DerivedSpecStale.__module__ = "kiro_crew.agent"


class ForeignAgentSpec(DerivedSpecStale):
    """A spec at a path this derivation owns was written by somebody else.

    The mirror path ``kirocrew-worker.json`` is a NAME, and a name can be claimed. A
    crew shared through the Fargate runtime installs its own spec into the same
    directory, so a file sitting at that path is not necessarily a mirror. A
    re-derivation that read one anyway would judge it a stale mirror of the default
    and replace the shared crew's prompt and tool surface with Kiro Crew's own, with
    no error and nothing in the logs naming the crew.

    A :class:`DerivedSpecStale` rather than a sibling of it, and the subclassing is
    load-bearing rather than tidy: every spawn-path caller catches that class BY NAME
    (``acp/client.py``, ``acp/runtime.py``, ``acp/harness/kas.py``) and turns it into a
    refused dispatch, so a separate exception type would reach them as an unhandled
    error and end the session instead of declining the spawn. What the subclass adds is
    a message and a log line naming the real fault; what it inherits is every caller's
    existing decision about a spec that cannot be vouched for.

    Both raise sites sit under callers that report rather than crash. The boot
    installer's failure costs the mirror, and the spawn gate then refuses the dispatch
    loudly rather than running an unverified spec; the spawn path's own re-derive
    returns ``False``.
    """


# Named by the path callers import it from, for the same reason as DerivedSpecStale.
ForeignAgentSpec.__module__ = "kiro_crew.agent"


def default_spec_fingerprint() -> str | None:
    """A content fingerprint of the mirrored surface of the installed default spec.

    CONTENT, not mtime. Two writes inside one filesystem timestamp tick, a restored
    backup, and a clock that steps backwards all produce a stale mirror with a
    plausible mtime, and each of those is a case where a revoked server would stay
    auto-approved on a worker. The hash covers exactly the keys the mirror copies --
    the same :data:`_WORKER_MIRRORED_SHAPES` map the derivation reads -- so an edit
    to a key the worker does not inherit does not force a pointless re-derive.

    ``None`` when the default spec is absent or unreadable: there is nothing to be
    stale against, and the caller treats that as "no check possible" rather than as
    a mismatch. Canonical JSON (sorted keys, no whitespace) so the same content
    hashes identically whichever writer produced it.
    """
    return _spec_fingerprint(_installed_default_spec())


def _spec_fingerprint(spec: dict[str, Any] | None) -> str | None:
    """The fingerprint of a spec ALREADY READ, so a caller can hash the bytes it used.

    Split from :func:`default_spec_fingerprint` because a caller that has the bytes must
    not go back to the file for their hash: the two reads are separate observations, and
    a write landing between them yields a fingerprint describing a generation the caller
    never saw. Every pairing of a fingerprint with a file identity goes through here.
    """
    if spec is None:
        return None
    mirrored = {key: spec[key] for key in _WORKER_MIRRORED_SHAPES if key in spec}
    mirrored = _without_volatile_mcp_env(mirrored)
    payload = json.dumps(mirrored, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _without_volatile_mcp_env(mirrored: dict[str, Any]) -> dict[str, Any]:
    """*mirrored* with per-launch MCP env nonce VALUES replaced, so they cannot move the hash.

    A launcher may re-stamp a nonce into an ``mcpServers`` entry's ``env`` on every
    session launch (``AIM_CREDS_AGENT_INJECTION`` is written into every agent spec the
    launcher manages). A nonce is not a grant: the ACP skill views already leave those keys out
    of their identity digests through :func:`kiro_crew.agent_spec_format.volatile_env_keys`,
    and this hash follows the same list. Hashing them made every concurrent session
    launch read as a trust change, so ``require_unchanged_derived_spec`` ended any
    worker whose load straddled one.

    Only the VALUE is normalized; the key stays in the hash. A default that drops the
    key altogether is a changed spec -- otherwise the mirror would keep an env entry
    absent from the default -- so it must still fingerprint differently. Every other
    env value still counts: a rotated credential is a different grant.

    Two checks read this fingerprint: the post-load bracket
    (``require_unchanged_derived_spec``) and the mirror-freshness check
    (``_derived_spec_matches_default``), which compares it against the fingerprint
    ``set_mirrored_from`` recorded at derive time. So a change to a volatile value alone
    also skips the mirror re-derive. That is safe: the launcher writes every agent spec it
    manages, the worker's own included, each with its own per-launch value, so the mirror
    never needs the default's copy.
    """
    servers = mirrored.get("mcpServers")
    if not isinstance(servers, dict):
        return mirrored
    volatile = volatile_env_keys()
    stripped: dict[str, Any] = {}
    for name, entry in servers.items():
        env = entry.get("env") if isinstance(entry, dict) else None
        if not isinstance(env, dict) or not any(str(key) in volatile for key in env):
            stripped[name] = entry
            continue
        stripped[name] = {
            **entry,
            "env": {
                key: (_VOLATILE_VALUE_SENTINEL if str(key) in volatile else value)
                for key, value in env.items()
            },
        }
    return {**mirrored, "mcpServers": stripped}


#: What a volatile env value hashes as. A fixed marker rather than omission, so the
#: key's presence is still part of the fingerprint while its per-launch value is not.
_VOLATILE_VALUE_SENTINEL = "<volatile>"


def default_spec_identity() -> str | None:
    """The installed default spec's file IDENTITY: ``st_mtime_ns``, size and inode.

    The only sound fast path for "has this file changed since I read it": an EQUALITY
    test on one file's own identity. Comparing the two specs' mtimes to each other is
    an ORDERING test, and ordering is exactly what a restored backup and a clock that
    steps backwards do not respect -- a default spec rolled back to an older copy is
    "older" than the mirror while holding different content, which is the hole the
    fingerprint exists to close. Size and inode ride along because a same-nanosecond
    rewrite is the ordinary case on a coarse clock, and an atomic replace swaps the
    inode.

    ``None`` when the file is absent or unstattable, which the caller reads as "no fast
    path available" and falls through to hashing.
    """
    return _file_identity(agent_mod.kiro_agents_dir_path() / AGENT_FILENAME)


def _file_identity(path: Path) -> str | None:
    """One file's own identity tuple, or ``None`` when it is absent or unstattable.

    Shared by the default spec and the derived mirror: both are bracketed by the same
    stat-read-stat rule, so both need the same notion of "the file I read a moment ago".
    """
    try:
        st = path.stat()
    except OSError:
        return None
    return f"{st.st_mtime_ns}-{st.st_size}-{st.st_ino}"


def _derived_spec_matches_default(agent: str) -> bool:
    """True only when *agent*'s mirror is PROVABLY the current default's.

    Two ways to establish it, cheapest first: the default spec is the same unchanged
    file instance this mirror was derived from (an identity test on one file), or its
    mirrored surface hashes to the fingerprint recorded at derive time.

    Raises rather than answering False when the default spec cannot be READ. False
    would send the caller into a re-derive, and a re-derive that cannot read the
    default silently produces a TEMPLATE-based worker -- a spec that looks freshly
    built while carrying none of the user's servers and none of their revocations. The
    two states are not interchangeable and only one of them is recoverable here.
    """
    identity = default_spec_identity()
    if (
        identity is not None
        and agent_state.get_mirrored_stat(agent) == identity
        # Re-stat AFTER the sidecar read. The recorded value is evidence about the file
        # the FIRST stat described, and reading it is itself a window: without this the
        # fast path can answer "provably current" about a default that has already been
        # replaced. Falling through on a mismatch reaches the hash comparison below,
        # which is the pessimistic direction.
        and default_spec_identity() == identity
    ):
        return True
    expected = default_spec_fingerprint()
    if expected is None:
        raise DerivedSpecStale(
            f"the default agent spec {agent_mod.kiro_agents_dir_path() / AGENT_FILENAME} exists but "
            "cannot be read (oversized, not JSON, or refused at the read gate), so the "
            f"{_WORKER_AGENT_FILENAME} mirror cannot be checked against it; refusing to "
            "start the worker on a mirror of unknown generation"
        )
    return agent_state.get_mirrored_from(agent) == expected


def require_fresh_derived_spec(
    agent: str | None, work_dir: str | Path | None
) -> "DerivedSpecSnapshot | None":
    """Refuse to spawn *agent* on a mirror older than the default spec. Repairs first.

    THE mechanism, and it is deliberately at the spawn rather than at the writers.
    ``kirocrew.json`` has six write sites across three modules under two different
    file locks (the rebuild, three app-registration paths, the dashboard MCP sync,
    the agent-config PUT), so a re-derive hung off each writer leaks one hole per
    writer nobody named -- which is how this arrived three rounds running. A check
    here is ONE place, covers a writer added tomorrow, and cannot lose the race a
    post-write hook can: it is not near the spawn, it IS the spawn.

    Cheap on the hot path: an identity comparison short-circuits before any hashing,
    and only the derived agents are examined at all, so every other spawn pays one
    string compare.

    Fails CLOSED, unlike its neighbour ``ensure_agent_materialized``, which is
    best-effort because a missing default spec costs a set_mode fallback. Here the
    stale spec is the hazard itself, so this raises :class:`DerivedSpecStale` and the
    caller aborts the spawn -- the same reasoning ``require_fork_governance`` applies
    to an unprojected fork.
    """
    if not agent or agent != Path(_WORKER_AGENT_FILENAME).stem:
        # SCOPE guard, not a freshness verdict: nothing else mirrors another spec, so
        # there is no generation to be stale against. Kept separate from the checks
        # below so "not applicable" can never be mistaken for "verified fresh".
        return None
    # What was just verified, for a caller that has to prove it STILL holds after a
    # subprocess has read the spec. ``None`` from the guard above and a snapshot here are
    # the two different things a caller must be able to tell apart.
    #
    # The pair -- and the derived spec itself -- is taken from ONE observation, bracketed
    # stat-read-stat around the whole
    # verification: identity first, the verification (and any re-derive) against that
    # same file, the fingerprint of the bytes read, then a re-stat proving the file never
    # moved. Assembling it from two observations -- an identity from a fresh stat beside a
    # fingerprint read back out of the sidecar -- yields a TORN pair, a NEW identity
    # carrying the OLD content's fingerprint, and the post-load check then accepts the new
    # default while the subprocess loaded the old spec. That is the exact failure the
    # bracket exists to catch, so the pair cannot come from the sidecar: the sidecar is
    # for the fast path that avoids a RE-DERIVE, and paying one hash of a small file on a
    # path that is already spawning a process is what buys coherence.
    worker_path = agent_mod.kiro_agents_dir_path() / _WORKER_AGENT_FILENAME
    for _ in range(_DEFAULT_SPEC_OBSERVATION_ATTEMPTS):
        identity = default_spec_identity()
        _require_fresh_worker_spec(work_dir)
        fingerprint = _spec_fingerprint(_installed_default_spec())
        # The derived spec is read HERE, inside the same window, and travels on the
        # snapshot. An in-process consumer that read it afterwards would be taking a
        # SECOND observation of a file this gate had already finished with, so a
        # revocation landing in between would reach the session as its whole tool
        # surface unchecked. Bracketed on its own identity too, because the bytes handed
        # out have to belong to the same instant as the verification that vouches for
        # them.
        worker_identity = _file_identity(worker_path)
        worker_spec = agent_mod._read_spec_capped(worker_path)
        if (
            identity is not None
            and fingerprint is not None
            and worker_identity is not None
            and worker_spec is not None
            and _file_identity(worker_path) == worker_identity
            and (default_spec_identity() == identity)
        ):
            return DerivedSpecSnapshot(identity, fingerprint, worker_spec)
    # Fails CLOSED on a file that will not hold still. A snapshot taken anyway would be
    # the torn pair above, and the bracket built on it would either accept a stale spec
    # or kill a valid session -- neither is better than refusing a spawn that is
    # recoverable and reportable.
    raise DerivedSpecStale(
        f"the default agent spec {agent_mod.kiro_agents_dir_path() / AGENT_FILENAME} or the "
        f"{_WORKER_AGENT_FILENAME} mirror kept changing while the mirror was being "
        f"verified, or the mirror could not be read "
        f"({_DEFAULT_SPEC_OBSERVATION_ATTEMPTS} attempts), so no coherent generation can "
        "be recorded and no verified spec can be handed to the session; refusing to "
        "start the worker"
    )


def require_unchanged_derived_spec(
    snapshot: "DerivedSpecSnapshot | None", *, agent: str | None = None
) -> None:
    """Prove the default spec has not changed since *snapshot* was taken. Fails closed.

    The second half of the bracket, called once the reader this process does not
    control has consumed the spec -- kiro-cli's ``initialize`` response is the earliest
    reliable signal of that. Any difference means the subprocess may have loaded a
    generation nobody verified, and the only sound answer is to end the session: the
    spec is already in another process's memory, so there is nothing left to repair.

    ``None`` short-circuits, because the pre-check answers ``None`` for every agent
    that mirrors nothing -- the bracket is not applicable rather than satisfied.

    Raises :class:`DerivedSpecStale` on any difference AND on a re-check that cannot be
    performed. An unreadable default here is not "probably fine": it is the one state
    in which this function cannot do its job, and the session it guards is already
    running on a spec it cannot vouch for.
    """
    if snapshot is None:
        return
    current_identity = default_spec_identity()
    if current_identity is not None and current_identity == snapshot.identity:
        return
    current_fingerprint = default_spec_fingerprint()
    if current_fingerprint is None:
        raise DerivedSpecStale(
            "the default agent spec became unreadable while the worker spec was being "
            "loaded, so the generation the session started on cannot be confirmed; "
            f"ending the session (verified {snapshot.fingerprint[:12]})"
        )
    if current_fingerprint != snapshot.fingerprint:
        raise DerivedSpecStale(
            "the default agent spec changed during worker load, so this session may "
            "have started on a spec nobody verified; ending it "
            f"(verified {snapshot.fingerprint[:12]}, now {current_fingerprint[:12]})"
        )


def _require_fresh_worker_spec(work_dir: str | Path | None) -> None:
    """Return only on POSITIVELY established freshness; raise on anything else.

    Written with NO ``return`` statement, which is the point: every earlier version of
    this check grew an early ``return`` for a case it could not evaluate -- a missing
    default, an unreadable one -- and each of those is a fail-OPEN pass on the one
    path where the mirror is unverifiable. Falling off the end is reachable only after
    a verified match or a re-derive that succeeded, so the shape carries the invariant
    instead of the reader having to audit each exit.

    A re-derive is itself positive establishment: it reads the installed default and
    writes the mirror inside one locked critical section, so on success the spec on
    disk was built from the default as it stood. That is what makes a missing or
    unwritable SIDECAR recoverable -- the bookkeeping is how freshness is proven
    cheaply next time, not what makes the spec correct -- while an unreadable DEFAULT
    is not, because there is nothing to derive from.
    """
    agent = Path(_WORKER_AGENT_FILENAME).stem
    shadow = agent_mod._project_shadow_of(agent, work_dir)
    if shadow is not None:
        # Checked FIRST, because everything below reasons about the global pair while
        # kiro-cli would resolve THIS file instead: a fresh, verified derivation in
        # ~/.kiro/agents proves nothing about the spec the session actually gets. A
        # checkout shipping its own worker spec can declare any ``autoApprove`` it
        # likes, and no derivation this module performs would ever touch it.
        #
        # Refused rather than repaired, and with no override knob: the file belongs to
        # the checkout, so rewriting it would be Crew editing a repository's tracked
        # content, and honouring it would let a cloned repo choose its own dispatched
        # worker's grants.
        raise DerivedSpecStale(
            f"the project checkout declares its own {agent} spec at {shadow}, which "
            "kiro-cli resolves ahead of the derived one; refusing to start the worker "
            "on a spec this derivation does not control"
        )
    agents_dir = agent_mod.kiro_agents_dir_path()
    default_path = agents_dir / AGENT_FILENAME
    if not default_path.exists():
        # A spawn needs the default spec present: the mirror is a function of it, and
        # with no default there is neither a way to verify the mirror nor a way to
        # rebuild it. A path that legitimately spawns before the default exists should
        # materialize it first -- the worker gate is not the place to make that legal.
        raise DerivedSpecStale(
            f"the default agent spec {default_path} is missing, so the "
            f"{_WORKER_AGENT_FILENAME} mirror cannot be verified or rebuilt; refusing "
            "to start the worker on a mirror of unknown generation"
        )
    # Attributed BEFORE freshness is judged, because "stale" is a statement about a
    # mirror and this file may not be one. A spec somebody else wrote is not stale
    # against the default and cannot be repaired by re-deriving over it, so asking the
    # freshness question first would make overwriting it look correct. The refusal is
    # raised here rather than left to the re-derive below so the message names the file
    # and the crew namespace: ``rederive_worker_agent`` never raises, so a refusal
    # reaching the spawn through it would arrive as "could not be re-derived", which
    # sends an operator looking for the wrong fault.
    mirror_path = agents_dir / _WORKER_AGENT_FILENAME
    _refuse_foreign_worker_spec(mirror_path, agent_mod._read_spec_capped(mirror_path))
    if not _derived_spec_matches_default(agent):
        # Attributed for the same reason as the foreign-spec refusal above, and
        # ahead of the re-derive rather than after it: where this data home does
        # not own the agents directory the write is refused, and because
        # ``rederive_worker_agent`` never raises, that refusal would arrive as
        # "could not be re-derived" -- a permanent dispatch outage described as a
        # generation mismatch, with nothing naming the directory or the remedy.
        if agent_mod._declined_foreign_spec_write(mirror_path):
            raise DerivedSpecStale(
                f"{mirror_path} mirrors an older generation of {default_path}, and this "
                f"instance's data home does not own {agents_dir}, so the mirror cannot be "
                "re-derived; refusing to start the worker rather than run grants absent "
                "from the default agent. To let this instance own its specs, remove the "
                "stale kirocrew*.json specs from that directory and restart"
            )
        agent_mod.logger.info(
            "Worker spec predates the default agent spec; re-deriving before spawn"
        )
        if not rederive_worker_agent("a stale mirror observed on the spawn path"):
            raise DerivedSpecStale(
                f"{agents_dir / _WORKER_AGENT_FILENAME} mirrors an older generation of "
                f"{default_path} and could not be re-derived; refusing to start the "
                "worker rather than run grants absent from the default agent"
            )


def rederive_worker_agent(reason: str) -> bool:
    """Re-derive ``kirocrew-worker.json`` after the DEFAULT spec changed out of band.

    The worker spec is a function of ``kirocrew.json``, and for most of its life the
    only writer of that file was ``rebuild_agent_config`` -- which re-derives the
    worker itself, so the mirror stayed current. App MCP registration is the other
    writer: it edits ``kirocrew.json`` in place under its own lock and returns. A
    trust REVOCATION therefore scrubbed the default spec and left the revoked stdio
    server mounted and auto-approved on the worker until the next gateway boot, which
    is exactly the window a dispatched worker runs in.

    ONE caller in the product: the spawn-path freshness gate
    (:func:`_require_fresh_worker_spec`), which re-derives a mirror it finds stale. The
    boot path calls :func:`_install_worker_agent` directly. Public and named anyway,
    because the next writer of ``kirocrew.json`` needs one obvious thing to call rather
    than a reason to rediscover this -- the spawn gate covers a writer nobody names,
    but a writer that CAN re-derive eagerly should not have to reach for a private
    installer to do it. Takes only a *reason* string, for the log: a caller that had
    to hand over a config or a path would be a caller that could hand over the WRONG
    one, and the whole point of the derivation is that it reads the installed default
    itself.

    **Must not be called while holding ``bridges._mcp_lock``.** The installer takes
    ``agents_spec_lock`` and then that lock, in that order, so a caller holding it
    already would invert the order this module establishes. A writer that calls this
    does so after its own MCP transaction has committed and released.

    Returns whether the re-derive ran. Best-effort and never raises: a failed
    re-derive leaves the previous worker spec in place, which is stale rather than
    broken, and must not fail the app operation that triggered it.
    """
    try:
        _install_worker_agent()
    except Exception:  # noqa: BLE001 — a stale worker spec must not fail a registration
        agent_mod.logger.warning("Worker agent re-derive failed after %s", reason, exc_info=True)
        return False
    agent_mod.logger.info("Re-derived worker agent config after %s", reason)
    return True


#: The shipped ``dashboard-template`` skill spec -- the ONE authoritative copy of the
#: dashboard author's charter. The installer reads its ``prompt`` and ``description`` from
#: here through :func:`parse_markdown_spec` rather than from a Python constant held equal
#: to it by nothing. ``parent.parent`` is the ``kiro_crew`` package root (this module is
#: ``kiro_crew/agent_materialization/worker_agent.py``).
_DASHBOARD_AUTHOR_SPEC_PATH: Path = (
    Path(__file__).resolve().parent.parent
    / "builtin_skills"
    / "kirocrew-dev"
    / "dashboard-template"
    / "agent-spec.md"
)

#: The agent NAME the dashboard-author spec declares. The install writes it as
#: ``config["name"]``; ownership is confirmed by the installer-recorded digest keyed on
#: this name (:func:`kiro_crew.agent_state.get_managed_digest`), not by the name itself.
_MANAGED_OWNED_NAME = "kirocrew-dashboard-author"


def _foreign_dashboard_author_spec_reason(spec: dict[str, Any] | None) -> str | None:
    """Why *spec*, read from the owned dashboard-author path, is not this installer's own
    managed write, or ``None`` when it REPRODUCES the installer-recorded ownership digest
    (so the installer may rewrite it).

    PROVENANCE by an installer-recorded digest in the ``agent_state`` sidecar, NOT by
    content marks. The dashboard-author stem was a user-creatable template name before it
    became owned, so a user could hand-place a ``kirocrew-dashboard-author.json`` -- and,
    as review found, so could a copy of ANOTHER owned agent (``kirocrew``, a conductor)
    renamed onto the stem, which would forge any mark chosen from the managed spec's own
    shape (its name, its ``kirocrew-core`` reference, even a narrowed server map). Content
    marks cannot tell such a duplicate apart from the managed spec. The digest can: it is
    recorded by the installer AFTER it lands a write (:func:`_install_dashboard_author_agent`
    calls :func:`kiro_crew.agent_state.set_managed_digest`), so only a file whose exact
    bytes reproduce that recorded value is confirmed as this installer's own last write.

    ``None`` -- "ours to rewrite" -- means exactly: *spec* is a dict whose canonical bytes
    (:func:`kiro_crew.agent_state.spec_digest`) equal the digest the installer recorded for
    this owned name. Everything else is foreign and left untouched:

    * NO digest is recorded (``get_managed_digest`` is ``None``) -- the installer never
      wrote here, so there is nothing to confirm and the present file is a user artefact;
    * *spec* is absent / unreadable / not a dict (the caller passes ``None``) -- a file
      whose provenance cannot even be parsed is never read as ours;
    * *spec*'s bytes do not reproduce the recorded digest -- it has been hand-edited, or it
      is a different agent's spec renamed onto the stem; either way not our last write.

    A crash between the write and the digest record leaves no digest, which fails CLOSED:
    the next rebuild sees "no ownership recorded" and declines rather than overwriting a
    file it cannot attribute. The install gate (:func:`_present_spec_blocks_install`)
    still refuses up front on an unreadable or symlinked file, so a broken file is covered
    there too.
    """
    if not isinstance(spec, dict):
        return None
    if not agent_state.managed_digest_matches(
        _MANAGED_OWNED_NAME, agent_state.spec_digest(spec), strict=True
    ):
        return "its bytes do not reproduce the installer-recorded ownership digest"
    return None


def _managed_dashboard_author_install_lands(path: Path) -> bool:
    """Would the managed install actually WRITE at *path* -- i.e. is the stem owned by a
    live installer that re-filters its grants every rebuild?

    True when :func:`_write_dashboard_author_spec` would land. The ``.json`` must not be
    blocked (absent, or it reproduces the installer-recorded ownership digest). A ``.md``
    sibling blocks only a FIRST-TIME install (an unconfirmed ``.json``, which the install
    must not write beside a user markdown spec); once the ``.json`` is CONFIRMED ours, the
    install refreshes it in place despite the sibling, so the fork-governance loop may treat
    it as owned (an owned writer re-filters its grants every rebuild). Mirrors the install
    gate exactly, so the two never disagree about whether this stem has a live writer.
    """
    if _present_spec_blocks_install(path) is not None:
        return False
    current_json = agent_mod._read_spec_capped(path) if path.is_file() else None
    json_confirmed = isinstance(current_json, dict) and _is_confirmed_managed_dashboard_author(
        current_json
    )
    if not json_confirmed:
        if _present_spec_blocks_install(path.parent / (path.stem + ".md")) is not None:
            return False
    return True


def _is_confirmed_managed_dashboard_author(spec: dict[str, Any] | None) -> bool:
    """Does *spec* POSITIVELY confirm as this installer's own managed dashboard-author?

    The fail-closed companion to :func:`_foreign_dashboard_author_spec_reason`, for the
    callers that must never strip a user's guards or skip filtering a user's approvals: the
    fork refresh, the capability-parent check and the governance filter. Those ask "is this
    confirmed OURS?", so an ABSENT, unreadable or non-dict spec (``None``) is confirmed as
    NOTHING -- it is treated as a user file (never refreshed, never guard-stripped, still
    filtered), NOT as "ours to rewrite". The install gate keeps using the reason helper,
    where a broken file IS replaceable so one truncated write cannot wedge the install
    forever; here the opposite default is the safe one.

    Confirmation is the installer-recorded ownership digest: *spec*'s canonical bytes
    reproduce the value :func:`kiro_crew.agent_state.get_managed_digest` holds for this
    owned name. No digest recorded, or bytes that do not reproduce it, is NOT confirmed.
    """
    if not isinstance(spec, dict):
        return False
    return _foreign_dashboard_author_spec_reason(spec) is None


def _install_dashboard_author_agent() -> None:
    """Generate and install the kirocrew-dashboard-author agent config.

    A dispatched, worker-shaped agent, but NOT the default-mirroring worker: it carries
    its own charter and its own tool surface, so it is built from scratch the way each
    conductor is rather than mirrored from the default spec. It authors ONE dashboard
    template and lands it as a pull request, so it mounts the two capabilities that work
    needs -- ``fs_write`` and ``execute_bash`` -- and nothing that would make it a
    conductor or a reporting worker: no ``session`` verb, no ``@kirocrew-work`` mount, no
    ``@kirocrew-dashboard`` publication surface, no ``code`` (governance classes it under
    ``filesystem.write``, a second path to two capabilities already mounted).

    ``fs_write`` and ``execute_bash`` are MOUNTED but never auto-approved, the line
    ``kirocrew-conductor`` draws and for the same reason: ``allowedTools`` has no argument
    matching, so a blanket write or shell grant cannot be told apart from "write anywhere"
    / "run anything", and the author's whole safety story is that a human reads its diff
    before it lands. Every auto-approved entry only READS or recalls -- the frontmatter
    ``allowedTools`` of the shipped ``agent-spec.md`` (``fs_read``, ``tool_search`` and the
    four read-only ``@kirocrew-core`` verbs) minus ``fs_read`` -- which is what makes
    granting it on the one path that never reaches the PreToolUse gate safe.

    Derived from the kirocrew agent so it inherits the resolved ``@kirocrew-core``
    invocation and the security hooks, then narrowed: the ``mcpServers`` map keeps only
    ``kirocrew-core`` (the one server any grant below names), and the KAS policy is
    derived from the FILTERED grant list rather than restated, so a ceiling that strips a
    grant strips its KAS rule with it. The shared writer version-gates it.

    The ``prompt``, ``description``, ``tools`` and ``allowedTools`` are ALL read from the
    shipped ``dashboard-template`` skill's ``agent-spec.md`` -- the one authoritative copy
    of the charter, which :func:`parse_markdown_spec` already parses from the package --
    rather than restated in Python literals that nothing holds equal to it. The
    frontmatter ``allowedTools`` is the SOURCE's authored auto-approve intent; the install
    drops ``fs_read`` from it (the one omission with a named reason below) and re-filters
    the rest through the governance ceiling, so what lands is the governed surface rather
    than a copy.
    """
    spec_source = parse_markdown_spec(_DASHBOARD_AUTHOR_SPEC_PATH.read_text(encoding="utf-8"))
    config = agent_mod.build_agent_config()
    config["name"] = _MANAGED_OWNED_NAME
    config["description"] = spec_source["description"]
    config["prompt"] = spec_source["prompt"]
    config["tools"] = list(spec_source["tools"])
    # ``allowedTools`` is the ONE path that never reaches the PreToolUse gate, so every
    # grant is filtered through the governance ceiling first -- a governed ref stays
    # MOUNTED (it is still in ``tools``) and simply prompts. The source frontmatter's
    # ``allowedTools`` is the authored auto-approve intent; we drop ``fs_read`` from it
    # because the ceiling withholds it from every derived spec's auto-approve list, so it
    # reaches the gate like ``fs_write`` and ``execute_bash`` do, and offering it only to
    # have it stripped would emit a withheld-audit event on every rebuild for a grant that
    # was never going to land. The rest is derived from the parsed spec rather than
    # restated, so the one authored list stays the single source of truth.
    config["allowedTools"] = auto_approve._filter_auto_approve(
        tuple(g for g in spec_source["allowedTools"] if g != "fs_read"),
        source="_install_dashboard_author_agent",
    )
    # Narrowed to the one server any grant above names. ``build_agent_config`` mounts the
    # whole managed set; a template is not published at run time, so the publication and
    # session-control servers are surface this charter cannot account for.
    mcp = config.get("mcpServers", {}) or {}
    core_entry = mcp.get("kirocrew-core")
    config["mcpServers"] = {"kirocrew-core": core_entry} if core_entry else {}
    # Derived from the FILTERED grant list rather than restated, so a ceiling that strips
    # a grant strips its KAS rule with it; the shared writer version-gates it.
    auto_approve._write_derived_permissions(
        config, config["allowedTools"], _DASHBOARD_AUTHOR_AGENT_FILENAME
    )
    agents_dir = agent_mod.kiro_agents_dir_path()
    agents_dir.mkdir(parents=True, exist_ok=True)
    path = agents_dir / _DASHBOARD_AUTHOR_AGENT_FILENAME
    # The whole read -> attribute -> write runs under ``agents_spec_lock``, the template-spec
    # writer lock every other read-modify-writer of this directory holds (the worker
    # installer, the reset path, the fork refresh, the dashboard PATCH). Without it two
    # overlapping rebuilds could interleave their reads and writes. Holding the lock makes
    # the ownership attribution and the write one atomic step, as the sibling worker
    # installer does.
    with agent_mod.agents_spec_lock(agents_dir):
        # Preserve the two AUTHORIZED, persisted user settings a rebuild would otherwise
        # discard, taken ONLY from a file this installer confirms is its own prior managed
        # write (so a user file at the stem contributes nothing): an explicit ``model`` pin
        # (``model_managed`` False, the dashboard PATCH / reset-model contract) and the user
        # ``resources`` skill mappings. Everything else is regenerated from the shipped spec
        # and the governance ceiling, so a grant the ceiling strips still goes. Read once,
        # under the lock, so the carry-over cannot race the write.
        existing = agent_mod._read_spec_capped(path)
        if isinstance(existing, dict) and _is_confirmed_managed_dashboard_author(existing):
            if agent_state.get_model_managed(_MANAGED_OWNED_NAME) is False:
                pinned = existing.get("model")
                if isinstance(pinned, str) and pinned.strip():
                    config["model"] = pinned
            # Mirror the confirmed spec's user skill mappings EXACTLY. "resources" PRESENT
            # (even an empty list) is the user's saved selection and is carried as-is; the
            # empty case matters -- an explicit remove-all-skills PATCH pops the key to [],
            # and carrying only a non-empty list would let the build's inherited default
            # (deep-merged from agent.json) restore the mapping the user removed. "resources"
            # ABSENT means the user holds no selection, so any inherited default is dropped.
            if "resources" in existing:
                carried = existing["resources"]
                if isinstance(carried, list):
                    config["resources"] = carried
            else:
                config.pop("resources", None)
        _write_dashboard_author_spec(path, config)


def _write_dashboard_author_spec(path: Path, config: dict) -> None:
    """Write the managed spec, attributing any file already at *path* by the INSTALLER-RECORDED
    OWNERSHIP DIGEST. Caller holds ``agents_spec_lock``.

    POSITIVE-CONFIRMATION by the digest recorded in the ``agent_state`` sidecar
    (:func:`_foreign_dashboard_author_spec_reason`): the spec is written only when the path
    is FREE, or the file there reproduces the digest the installer recorded for its last
    managed write, i.e. our own prior landed write. A user who hand-placed a spec at this
    once-user-creatable stem -- or a copy of another owned agent renamed onto it -- is left
    untouched, because its bytes do not reproduce the recorded digest. The digest is recorded
    AFTER this write (below), under this same lock, so the record and the file move together.

    Three branches, exactly:

    * path ABSENT -> install (nothing to lose).
    * path PRESENT and the file reproduces the recorded ownership digest -> refresh is
      allowed (that digest IS the record that this is our own prior write).
    * path PRESENT not reproducing the digest (no digest recorded, hand-edited, or a
      different agent's spec renamed on), OR the file cannot be read -> leave it untouched
      and log once. We never overwrite a file we cannot positively confirm is our own write.

    BOTH candidate forms are checked before the name is claimed: kiro-cli reads
    ``<stem>.json`` and the v3 engine also reads ``<stem>.md``, and a ``.json`` written
    beside a user's ``.md`` would SHADOW that markdown spec. So a user ``.md`` at the stem
    blocks the install exactly as an unconfirmed ``.json`` does -- the installer never
    shadows a spec it did not write.
    """
    agents_dir = path.parent
    stem = path.stem
    md_candidate = agents_dir / (stem + ".md")
    # Is the ``.json`` already our confirmed managed write? Decide this FIRST, because it
    # changes whether a ``.md`` sibling may block. A ``.md`` at the stem must block a
    # FIRST-TIME install (writing the ``.json`` would shadow the user's markdown spec), but
    # it must NOT freeze an already-confirmed managed ``.json``: refusing to refresh that
    # ``.json`` would leave its auto-approvals live after the ceiling revoked them, since a
    # non-fork managed spec has no other writer to re-filter it. So a confirmed ``.json`` is
    # refreshed in place and the sibling ``.md`` is left untouched.
    current_json = agent_mod._read_spec_capped(path) if path.is_file() else None
    json_confirmed = isinstance(current_json, dict) and _is_confirmed_managed_dashboard_author(
        current_json
    )
    json_blocked = _present_spec_blocks_install(path)
    if json_blocked is not None:
        agent_mod.logger.warning(
            "Not installing the managed %s: %s. Leaving it untouched; remove or rename "
            "that file to let the managed agent install.",
            _DASHBOARD_AUTHOR_AGENT_FILENAME,
            json_blocked,
        )
        return
    # The ``.md`` sibling blocks only a first-time install (unconfirmed ``.json``). Once the
    # ``.json`` is confirmed ours, the ``.md`` does not stop the refresh.
    if not json_confirmed:
        md_blocked = _present_spec_blocks_install(md_candidate)
        if md_blocked is not None:
            agent_mod.logger.warning(
                "Not installing the managed %s: %s. Leaving it untouched; remove or rename "
                "that file to let the managed agent install.",
                _DASHBOARD_AUTHOR_AGENT_FILENAME,
                md_blocked,
            )
            return
    # Every candidate is free, or the ``.json`` reproduces the installer-recorded ownership
    # digest (so a refresh is allowed). Two-phase digest commit so a crash never leaves the
    # bytes and the record out of step: record the NEW bytes' digest as PENDING first, then
    # replace the file (``_atomic_json_write`` -- a tmp-file + atomic replace), then finalize.
    # At every instant the file reproduces either the old finalized digest or the pending
    # one, so a later rebuild always re-confirms and re-filters instead of freezing.
    new_digest = agent_state.spec_digest(config)
    current_digest = (
        agent_state.spec_digest(current_json) if isinstance(current_json, dict) else None
    )
    agent_state.begin_managed_write(_MANAGED_OWNED_NAME, new_digest, current=current_digest)
    agent_mod._atomic_json_write(path, config)
    agent_state.finalize_managed_write(_MANAGED_OWNED_NAME)
    agent_mod.logger.info("Installed dashboard-author agent config: %s", path)


def _present_spec_blocks_install(candidate: Path) -> str | None:
    """Why a file already at *candidate* (a ``.json`` or ``.md`` at the dashboard-author
    stem) must block the managed install, or ``None`` when it does not.

    ``None`` means: the candidate is ABSENT, or it is a ``.json`` that reproduces the
    installer-recorded ownership digest (our own prior write, which the refresh may
    overwrite). A reason string means leave everything untouched:

    * a symlink / non-regular file -> never follow or clobber;
    * a read failure (oversized, non-UTF-8, unparseable) -> provenance unconfirmable;
    * a file that does not reproduce the recorded digest -> a user artefact or another
      agent's spec (its ``.json`` is not our write; ANY ``.md`` is a user spec, since the
      installer only ever writes ``.json``, so even a ``.md`` that reproduced the digest
      would still be the user's and is left untouched).

    The digest-reproducing ``.json`` refresh case is the ONE overwrite the installer makes;
    everything else is protected.
    """
    if not os.path.lexists(candidate):
        return None
    if candidate.is_symlink() or not candidate.is_file():
        return f"{candidate} is not a regular file"
    current = agent_mod._read_spec_capped(candidate)
    if current is None:
        return f"the file at {candidate} could not be read, so its provenance cannot be confirmed"
    # The installer only ever writes the ``.json`` form, so a ``.md`` is never its own write:
    # a ``.md`` is always a user spec and must be left untouched. Only a ``.json`` may be
    # confirmed as ours and refreshed.
    if candidate.suffix.lower() != ".json":
        return f"a user markdown spec exists at {candidate}"
    reason = _foreign_dashboard_author_spec_reason(current)
    if reason is not None:
        return f"a file at {candidate} is not this installer's own ({reason})"
    return None
