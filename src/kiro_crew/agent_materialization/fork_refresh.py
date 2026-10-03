"""The governance refresh of crews' private template copies.

A fork of an owned template carries the template's machine plumbing and grants, so
every rebuild re-projects both onto it. The refresh runs under the settled event
:func:`kiro_crew.agent.require_fork_governance` waits on, and records every fork it
could not re-filter in :data:`_fork_refresh_failed`, which that gate refuses to start
a session on. The boot path defers the refresh to a thread
(:func:`refresh_after_rebuild`); the gate holds fork-backed spawns until it finishes.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Literal

from kiro_crew import agent as agent_mod
from kiro_crew import agent_state, user_json
from kiro_crew.agent_files import DASHBOARD_AUTHOR_AGENT_FILENAME, OWNED_KIRO_AGENT_FILES
from kiro_crew.agent_materialization import auto_approve
from kiro_crew.agent_spec_format import is_markdown_spec

#: The dashboard-author origin stem, sidecar-gated in :func:`_origin_is_owned`.
_DASHBOARD_AUTHOR_STEM = Path(DASHBOARD_AUTHOR_AGENT_FILENAME).stem


def refresh_after_rebuild(
    refresh_forks: bool | Literal["defer"], gated_off: frozenset[str]
) -> None:
    """Run, defer or skip the fork refresh that follows a rebuild."""
    # Keep crews' private template copies (forks of owned templates)
    # machine-maintained — same reason kirocrew.json itself is refreshed.
    # "defer" is the boot path: per-fork work scales with fork count and must
    # not delay readiness. Owning the deferral HERE keeps the skip+schedule
    # pair in one place, so no caller can skip the refresh and forget the
    # background half (or drop gated_off, as the first split version did).
    if refresh_forks == "defer":
        # The whole refresh — plumbing AND the governance projection — stays
        # off the boot path (no-new-work-on-gateway-boot-path: the per-fork
        # pass scales with fork count). Sessions do not get to race it either:
        # the settled event is cleared here and ensure_agent_materialized
        # holds a fork-backed spawn until the pass re-sets it, so a fork
        # carrying grants the ceiling has since tightened away is re-filtered
        # before any session consumes it.
        _fork_refresh_settled.clear()

        def _run_deferred() -> None:
            global _fork_refresh_failed
            try:
                _refresh_forked_templates(gated_off=gated_off)
            except Exception:
                # The pass died before per-fork accounting: no fork can be
                # trusted as refreshed, so all fork-backed spawns stay blocked.
                # The event is NOT set here: the wrapper's own finally already
                # re-set it if this was the last pending pass, and setting it
                # unconditionally would bypass the pending-pass counter.
                _fork_refresh_failed = frozenset({"*"})
                agent_mod.logger.warning("deferred fork refresh failed", exc_info=True)

        try:
            threading.Thread(target=_run_deferred, name="fork-refresh", daemon=True).start()
        except Exception:
            # A thread that never started can never set the event; leaving it
            # cleared would hold every fork spawn for the full wait budget.
            # Recorded as a pass-level failure FIRST: with no pass ever run,
            # an open gate over an empty failure set would spawn forks on
            # never-re-filtered grants — the one fail-open among siblings
            # that all record "*" (Opus round-47).
            global _fork_refresh_failed
            _fork_refresh_failed = frozenset({"*"})
            _fork_refresh_settled.set()
            raise
    elif refresh_forks:
        try:
            _refresh_forked_templates(gated_off=gated_off)
        except Exception:
            agent_mod.logger.debug("forked template refresh failed", exc_info=True)


# Serializes refresh passes and scopes the settled-event lifecycle: the event
# is cleared for the COMPLETE duration of any refresh — boot-deferred or
# synchronous — and set only after per-fork accounting has been recorded.
_fork_refresh_lock = threading.Lock()

# Refresh passes registered but not yet finished, adjusted OUTSIDE the pass
# lock (own lock below): a queued pass must drop the settled event before it
# can even contend for the pass lock, and the event is re-set only when the
# LAST pending pass finishes — otherwise the first of two overlapping passes
# would re-open the spawn gate on grants the queued pass has not re-filtered.
_fork_refresh_pending = 0
_fork_refresh_count_lock = threading.Lock()

# Set while no fork refresh is in progress. Cleared by _refresh_forked_templates
# for its complete lifecycle (and by the boot deferral before its thread starts,
# to close the pre-start window), so require_fork_governance holds fork-backed
# spawns until governance has been re-projected and accounted.
_fork_refresh_settled = threading.Event()
_fork_refresh_settled.set()

# Fork names whose LAST refresh attempt failed, with "*" meaning the pass died
# before per-fork accounting. Assigned whole (never mutated in place) by
# _refresh_forked_templates and the deferred runner, read by
# require_fork_governance — a fork in this set may NOT start a session, because
# its on-disk allowedTools/autoApprove were never re-filtered against the
# current ceiling and neither ever reaches the PreToolUse gate.
_fork_refresh_failed: frozenset[str] = frozenset()

# Bounded so the spawn path's never-hangs contract survives a wedged refresh
# thread; a module constant so tests can shrink it. A timeout is treated as a
# FAILURE (spawn aborted), never as a release.
_FORK_REFRESH_WAIT_SECS = 60.0


def _refresh_forked_templates(*, gated_off: "frozenset[str] | None" = None) -> None:
    """Refresh every fork under the spawn gate: the settled event stays
    cleared for the COMPLETE pass — synchronous callers (rebind, setup)
    included, not just the boot deferral — and is re-set only when the LAST
    pending pass finishes, so overlapping passes cannot re-open the gate on
    grants the queued pass has not yet re-filtered."""
    global _fork_refresh_failed, _fork_refresh_pending
    # Registered BEFORE the pass lock: a queued pass must drop the settled
    # event immediately, otherwise the pass currently finishing would set it
    # and open a window where a spawn consumes grants the queued pass — the
    # one carrying the policy change that triggered it — has not re-filtered.
    with _fork_refresh_count_lock:
        _fork_refresh_pending += 1
        _fork_refresh_settled.clear()
    try:
        with _fork_refresh_lock:
            try:
                _refresh_forked_templates_locked(gated_off=gated_off)
            except Exception:
                # The pass died before per-fork accounting — including a STRICT
                # sidecar read refusing a corrupt file. No fork can be trusted as
                # refreshed, so all fork-backed spawns stay blocked; recorded HERE
                # so synchronous callers (rebind, setup) fail closed exactly like
                # the boot deferral.
                _fork_refresh_failed = frozenset({"*"})
                raise
    finally:
        with _fork_refresh_count_lock:
            _fork_refresh_pending -= 1
            if _fork_refresh_pending == 0:
                _fork_refresh_settled.set()


def _refresh_forked_templates_locked(*, gated_off: "frozenset[str] | None" = None) -> None:
    """Refresh machine-maintained fields in every fork of an owned template.

    A fork copies the built-in template verbatim, including plumbing setup
    recomputes on every run: managed MCP server commands (absolute interpreter
    paths), security hooks, the data-home pin. Frozen, that plumbing rots
    silently — a stale interpreter path stops every managed tool from starting.
    So forks get the same merge-preserving refresh ``kirocrew.json`` gets, in
    ``fork`` mode (human-edited fields untouched; see _refresh_dynamic_fields).

    Only forks whose origin CHAIN reaches a Kiro Crew-owned template get the
    PLUMBING refresh: a fork of a user's custom template inherits no machine
    plumbing (setup never composes non-owned specs), and refreshing it would
    stamp kirocrew's prompt and hooks onto an unrelated spec. The GOVERNANCE
    passes (ceiling + auto-approve strip) run for every corroborated fork
    regardless of origin — no other writer sanitizes these files.
    """
    forks = agent_state.all_fork_info()
    global _fork_refresh_failed
    if not forks:
        _fork_refresh_failed = frozenset()
        return
    owned_names = {Path(f).stem for f in OWNED_KIRO_AGENT_FILES}
    # Defense in depth: the sidecar is sealed read-only for sandboxed agents and
    # its writers are gated, but lineage alone must still never drive a write —
    # a fork qualifies only when config.json corroborates it, i.e. the crew
    # named by ``private_to`` is actually bound to this spec.
    try:
        from kiro_crew.config.loader import KiroCrewConfig  # circular import

        cfg_agents = KiroCrewConfig.load().agents
    except Exception:
        # No corroboration possible means no fork was refreshed: every
        # fork-backed session stays blocked rather than running stale grants.
        _fork_refresh_failed = frozenset({"*"})
        agent_mod.logger.warning("fork refresh skipped: config unreadable", exc_info=True)
        return

    def _binding_corroborates(name: str) -> bool:
        crew = forks[name].get("private_to")
        bound = cfg_agents.get(crew) if isinstance(crew, str) else None
        return bound is not None and bound.kiro_agent == name

    def _origin_is_owned(name: str) -> bool:
        seen: set[str] = set()
        while name in forks and name not in seen:
            seen.add(name)
            name = forks[name]["forked_from"]
        if name not in owned_names:
            return False
        # The dashboard-author stem was a user-creatable template name before it became
        # owned, so a pre-upgrade fork can descend from a USER template at this stem.
        # Treating that origin as owned here would overwrite the fork's hooks and MCP
        # plumbing with the managed set. Count it as an owned origin ONLY when the sidecar
        # positively confirms the origin spec on disk is ours -- the same gate the installer,
        # the hook sweep and the home probe apply. Other owned origins keep the plain check.
        if name == _DASHBOARD_AUTHOR_STEM:
            origin_path = agent_mod.kiro_agents_dir_path() / (name + ".json")
            return agent_state.managed_owned_matches(name, origin_path) is True
        return True

    agents_dir = agent_mod.kiro_agents_dir_path()
    failures: set[str] = set()
    for fork_name in sorted(forks):
        # Owned specs have their own writer; this path must never touch them.
        if fork_name in owned_names:
            continue
        if not _binding_corroborates(fork_name):
            # Defense in depth (the sidecar is sealed and its writers gated):
            # lineage alone must never drive a write to a spec file —
            # governance included. But an ORPHANED fork
            # (lineage with no crew binding) also cannot be trusted as
            # refreshed: its grants were never re-filtered, so record it as a
            # failure — no write happens, require_fork_governance simply
            # refuses to start sessions on it. Self-healing: rebinding a crew
            # triggers a refresh, which corroborates and clears the record.
            failures.add(fork_name)
            continue
        # Origin gates ONLY the plumbing refresh: setup never composes
        # non-owned specs, so a custom-template fork inherits no machine
        # plumbing. Governance is origin-independent — a corroborated fork's
        # allowedTools/autoApprove face the same ceiling regardless of what it
        # was forked from, and no other writer sanitizes these files, so
        # skipping them here would leave stale grants live past a tightening.
        plumb = _origin_is_owned(fork_name)
        # The WHOLE per-fork body is fenced: one fork's failure is recorded and
        # the loop moves on, so a mid-loop error can neither strand the later
        # forks unrefreshed nor release this one's session gate — a fork in
        # `failures` is refused by require_fork_governance.
        try:
            if agent_state.get_capabilities(fork_name) is not None:
                from kiro_crew.agent_capabilities import reconcile_member_capabilities

                reconcile_member_capabilities(forks[fork_name]["private_to"])
                continue
            # Resolve the ACTUAL spec file (declared name wins over the stem,
            # same as every other resolver) rather than reconstructing
            # `<name>.json`: a stem/name divergence would otherwise make the
            # refresh silently skip the real file and leave its grants stale.
            try:
                spec_path = agent_mod.agent_spec_path(fork_name)
            except ValueError:
                # Two specs declare this name — which is live is undefined, so
                # neither can be trusted as refreshed. Fail closed.
                failures.add(fork_name)
                agent_mod.logger.warning("fork refresh: ambiguous spec name %r", fork_name)
                continue
            if spec_path is None:
                # No spec on disk: nothing carries grants, nothing to refresh.
                continue
            if is_markdown_spec(spec_path):
                # Forks are JSON copies Kiro Crew wrote; a markdown file that
                # resolves as one cannot be re-serialized, so its grants cannot
                # be refreshed. Fail closed like an unreadable spec.
                failures.add(fork_name)
                agent_mod.logger.warning(
                    "fork refresh: %r resolves to a markdown spec %s, which this writer "
                    "cannot rewrite; treating its grants as unrefreshed",
                    fork_name,
                    spec_path,
                )
                continue
            # The whole read-modify-write sits under the shared spec lock: a
            # refresh that reads, loses the CPU to a dashboard PATCH, then
            # writes its stale snapshot would silently revert the user's edit.
            with agent_mod.agents_spec_lock(agents_dir):
                # A strict read: `_load_json` answers `{}` for an unreadable
                # file, which would pass the check below and be written back
                # over the spec.
                try:
                    config = user_json.loads_user_json(spec_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    config = None
                if not isinstance(config, dict):
                    # Unreadable spec: governance cannot be projected onto it.
                    failures.add(fork_name)
                    continue
                if plumb:
                    try:
                        agent_mod._refresh_dynamic_fields(config, gated_off=gated_off, fork=True)
                    except Exception:
                        # Plumbing rot is recoverable; the governance passes
                        # below still run and write, so a plumbing bug never
                        # leaves stale grants on disk.
                        agent_mod.logger.debug(
                            "refresh failed for forked template %r", fork_name, exc_info=True
                        )
                # Governance passes, same as every other spec writer:
                # allowedTools and autoApprove are the two paths that never
                # reach the PreToolUse gate, so a fork carrying grants the
                # ceiling later tightened against must be re-filtered on every
                # refresh — this writer is exactly where a stale grant would
                # otherwise persist verbatim.
                auto_approve._apply_allowed_tools_ceiling(
                    config, source=f"fork-refresh:{fork_name}"
                )
                servers_map = config.get("mcpServers")
                if isinstance(servers_map, dict):
                    config["mcpServers"] = auto_approve._strip_ungoverned_auto_approve(servers_map)
                agent_state.lift_and_strip_bookkeeping(config, fork_name)
                agent_mod._atomic_json_write(spec_path, config)
        except Exception:
            failures.add(fork_name)
            agent_mod.logger.warning(
                "fork refresh failed for %r; its sessions stay blocked", fork_name, exc_info=True
            )
    _fork_refresh_failed = frozenset(failures)
