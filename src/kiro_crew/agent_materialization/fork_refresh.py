"""The governance refresh of crews' private template copies.

A fork of an owned template carries the template's machine plumbing and grants, so
every rebuild re-projects both onto it. The refresh runs under the settled event
:func:`kiro_crew.agent.require_fork_governance` waits on, and records every fork it
could not re-filter in :data:`_fork_refresh_failed`, which that gate refuses to start
a session on. The boot path defers the refresh to a thread
(:func:`refresh_after_rebuild`); the gate holds fork-backed spawns until it finishes.
"""

from __future__ import annotations

import contextlib
import copy
import re
import stat
import threading
from pathlib import Path
from typing import Literal

from kiro_crew import agent as agent_mod
from kiro_crew import agent_state, user_json
from kiro_crew.agent_files import DASHBOARD_AUTHOR_AGENT_FILENAME, OWNED_KIRO_AGENT_FILES
from kiro_crew.agent_materialization import auto_approve
from kiro_crew.agent_spec_format import is_markdown_spec

#: The dashboard-author origin stem, digest-gated in :func:`_origin_is_owned`.
_DASHBOARD_AUTHOR_STEM = Path(DASHBOARD_AUTHOR_AGENT_FILENAME).stem

#: The SEL operation :func:`_report_mcp_json_merge_pinned_off` records.
MCP_JSON_MERGE_PINNED_OP = "fork_mcp_json_merge_disabled"


def _report_mcp_json_merge_pinned_off(fork_name: str, spec_path: Path) -> None:
    """Say that a refresh turned off a fork's global ``mcp.json`` merge, and the remedy.

    kiro-cli reads an absent ``includeMcpJson`` as true, so a fork that carried true
    or no value reached every server in the global ``mcp.json``. The governed tail
    pins it ``False``, and a server the fork reached only that way is absent from its
    next session. The warning and the SEL record name the fork and tell the user to
    list the server in the fork's own ``mcpServers``. The caller reports only a flip,
    so a fork already at ``False`` records nothing. Best-effort: an audit failure
    never fails the refresh.
    """
    message = (
        f"fork refresh: {fork_name!r} ({spec_path.name}) had includeMcpJson true or unset; "
        "the refresh set it to false, so servers from the global mcp.json are not loaded "
        "for this agent. List any server it needs in the fork's own mcpServers."
    )
    agent_mod.logger.warning("%s", message)
    try:
        agent_mod.sel().log_api_access(
            caller="system",
            operation=MCP_JSON_MERGE_PINNED_OP,
            outcome="ok",
            source=f"fork-refresh:{fork_name}",
            resources=message,
        )
    except Exception:  # noqa: BLE001 — the audit must not break the refresh
        agent_mod.logger.debug("SEL audit unavailable for the mcp.json merge pin", exc_info=True)


def _dashboard_author_file_is_installers(path: Path) -> bool:
    """True ONLY when the file at *path* positively confirms as the managed dashboard-author
    spec -- its bytes reproduce the installer-recorded ownership digest.

    Fail-closed: an ABSENT or unreadable file -- for example a user-owned ``.md`` at the stem
    with a JSON crew fork, where the capped reader returns ``None`` -- is NOT confirmed ours,
    so this returns False and the fork refresh leaves the fork's custom ``preToolUse`` guards
    in place rather than replacing them with bundled hooks. A lazy import avoids an import
    cycle at module load."""
    from kiro_crew.agent_materialization import worker_agent

    spec = agent_mod._read_spec_capped(path)
    return worker_agent._is_confirmed_managed_dashboard_author(spec)


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
                if _shared_template_held:
                    # The boot rebuild has already returned, so its hold memo is
                    # set here for the maintenance wake's retry.
                    agent_mod._conductor_spec_held = True
            except Exception:
                # The pass died before per-fork accounting: no fork can be
                # trusted as refreshed, so all fork-backed spawns stay blocked.
                # The event is NOT set here: the wrapper's own finally already
                # re-set it if this was the last pending pass, and setting it
                # unconditionally would bypass the pending-pass counter.
                _fork_refresh_failed = frozenset({"*"})
                agent_mod._conductor_spec_held = True
                agent_mod.logger.warning("deferred fork refresh failed", exc_info=True)

        try:
            threading.Thread(target=_run_deferred, name="fork-refresh", daemon=True).start()
        except Exception:
            # A thread that never started can never set the event; leaving it
            # cleared would hold every fork spawn for the full wait budget.
            # Recorded as a pass-level failure FIRST: with no pass ever run,
            # an open gate over an empty failure set would spawn forks on
            # never-re-filtered grants — the one fail-open among siblings
            # that all record "*".
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

# True when the last pass left a crew-bound shared template unfiltered for a reason a
# retry can clear (see _govern_bound_shared_specs). Read by the rebuild, which folds it
# into its conductor hold so a tightened ceiling is retried rather than marked projected.
_shared_template_held: bool = False

# Set when the last pass's hold rests ONLY on a config file its load could not read
# whole: the governance answer generation the pass ran under and the loader's
# fingerprint of both config files. None when nothing is held or the hold has another
# cause (a capability reconcile or the pass itself raised), which a retry may clear with
# nothing on disk changing. Assigned whole, cleared at the start of every pass. Read by
# shared_template_hold_unchanged.
_shared_template_hold_record: tuple[int, tuple] | None = None

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
    global _fork_refresh_failed, _shared_template_held
    global _shared_template_hold_record
    # Registered BEFORE the pass lock (``hold_spawn_gate`` does the accounting): a
    # queued pass must drop the settled event immediately, otherwise the pass
    # currently finishing would set it and open a window where a spawn consumes
    # grants the queued pass -- the one carrying the policy change that triggered
    # it -- has not re-filtered.
    with hold_spawn_gate():
        with _fork_refresh_lock:
            try:
                _refresh_forked_templates_locked(gated_off=gated_off)
            except Exception:
                # The pass died before per-fork accounting -- including a STRICT
                # sidecar read refusing a corrupt file. No fork can be trusted as
                # refreshed, so all fork-backed spawns stay blocked; recorded HERE
                # so synchronous callers (rebind, setup) fail closed exactly like
                # the boot deferral. The shared-template pass did not run either.
                _fork_refresh_failed = frozenset({"*"})
                _shared_template_hold_record = None
                _shared_template_held = True
                raise


@contextlib.contextmanager
def hold_spawn_gate():
    """Keep fork-backed session starts waiting while the caller changes a copy.

    The one place the spawn gate is accounted: a pending count and a cleared
    settled event, set again only when the LAST holder leaves. A refresh pass
    holds it, and so does a writer that drops a private copy's saved intent
    (across the write and the refresh it triggers), so a session cannot start
    in between on grants the refresh has not yet re-filtered.
    """
    global _fork_refresh_pending
    with _fork_refresh_count_lock:
        _fork_refresh_pending += 1
        _fork_refresh_settled.clear()
    try:
        yield
    finally:
        with _fork_refresh_count_lock:
            _fork_refresh_pending -= 1
            if _fork_refresh_pending == 0:
                _fork_refresh_settled.set()


def _apply_governance_passes(config: dict, *, source: str, audit: bool = True) -> None:
    """Re-filter the two grant lists kiro-cli honours before the PreToolUse gate, in place.

    ``allowedTools`` goes through the ceiling and every governed server loses its
    ``autoApprove``. A fork gets the same two inside the governed tail
    (``auto_approve.govern_spec``); the kiro-cli start check runs them on a copy of a
    shared template to ask whether they would remove anything
    (:func:`require_shared_template_within_ceiling`), and never writes the result.
    """
    auto_approve._apply_allowed_tools_ceiling(config, source=source, audit=audit)
    servers_map = config.get("mcpServers")
    if isinstance(servers_map, dict):
        config["mcpServers"] = (
            auto_approve._strip_ungoverned_auto_approve(servers_map)
            if audit
            else auto_approve._strip_ungoverned_auto_approve(servers_map, audit=False)
        )


def _owned_spec_has_its_own_writer(name: str, owned_names: "set[str]") -> bool:
    """Does the spec named *name* genuinely have an owned-spec writer that re-filters
    its grants, so the governance passes here may skip it?

    A plain owned stem (worker, conductor, service agents) always does -- its installer
    runs every rebuild. The dashboard-author stem is the exception: it was a
    user-creatable template name before it became owned, so a pre-upgrade PRIVATE COPY
    can sit at this stem with NO owned writer re-filtering it. Skipping such a file as
    "owned" would leave its ``allowedTools``/``autoApprove`` live against a tightened
    ceiling forever (``require_fork_governance`` then admits sessions on it). So for
    this stem the skip is honoured ONLY when the managed install would actually LAND on
    the ``.json`` -- it reproduces the installer-recorded ownership digest AND no user
    ``.md`` sibling makes the installer refuse. A private copy that reproduces the digest
    but sits beside a ``.md`` (so the installer refuses and never re-filters it), or any
    unconfirmed copy, falls through to the governance passes below rather than being
    skipped as owned.
    """
    if name not in owned_names:
        return False
    if name == _DASHBOARD_AUTHOR_STEM:
        from kiro_crew.agent_materialization import worker_agent

        spec_path = agent_mod.kiro_agents_dir_path() / (name + ".json")
        return worker_agent._managed_dashboard_author_install_lands(spec_path)
    return True


def fork_origin_is_owned(name: str, forks: "dict | None" = None) -> bool:
    """Would the fork refresh re-apply owned plumbing to the fork *name*?

    Follows the lineage in *forks* (``agent_state.all_fork_info()`` when omitted)
    to its origin and asks whether that origin's NAME is an owned template. Name
    alone decides, whatever scope the origin was read from: this is the rule
    ``_refresh_forked_templates_locked`` applies, and any writer that drops a
    member's inheritance intent must ask the same question, or the refresh
    restores plumbing the member removed. The dashboard-author stem was a
    user-creatable template name before it became owned, so a pre-upgrade fork
    can descend from a USER template at this stem. Treating that origin as
    owned here would overwrite the fork's hooks and MCP plumbing with the
    managed set. Count it as an owned origin ONLY when the on-disk origin spec
    reproduces the installer-recorded ownership digest -- the same gate the
    installer, the capability-parent check and the home probe apply. Other
    owned origins keep the plain check.
    """
    if forks is None:
        forks = agent_state.all_fork_info()
    owned_names = {Path(f).stem for f in OWNED_KIRO_AGENT_FILES}
    seen: set[str] = set()
    while name in forks and name not in seen:
        seen.add(name)
        name = forks[name]["forked_from"]
    if name not in owned_names:
        return False
    if name == _DASHBOARD_AUTHOR_STEM:
        origin_path = agent_mod.kiro_agents_dir_path() / (name + ".json")
        return _dashboard_author_file_is_installers(origin_path)
    return True


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

    After the forks, every other spec a crew in ``config.json`` is bound to gets its
    capability reconcile (:func:`_govern_bound_shared_specs`). Its file is never
    written: Kiro Crew does not own it, so a kiro-cli start on one the ceiling would
    narrow is refused instead (:func:`require_shared_template_within_ceiling`).
    """
    forks = agent_state.all_fork_info()
    global _fork_refresh_failed, _shared_template_held, _shared_template_hold_record
    # Every exit below that holds without recording paths holds for a cause no file
    # change marks, so the record from an earlier pass must not outlive this one.
    _shared_template_hold_record = None
    owned_names = {Path(f).stem for f in OWNED_KIRO_AGENT_FILES}
    # Defense in depth: the sidecar is sealed read-only for sandboxed agents and
    # its writers are gated, but lineage alone must still never drive a write —
    # a fork qualifies only when config.json corroborates it, i.e. the crew
    # named by ``private_to`` is actually bound to this spec.
    try:
        from kiro_crew.config.loader import KiroCrewConfig  # circular import

        cfg = KiroCrewConfig.load()
        cfg_agents = cfg.agents
    except Exception:
        # The shared-template pass needs the bindings too, so it is skipped and
        # held for a retry.
        _shared_template_held = True
        if not forks:
            # No fork exists to block.
            _fork_refresh_failed = frozenset()
            agent_mod.logger.warning(
                "shared template governance skipped: config unreadable", exc_info=True
            )
            return
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
        return fork_origin_is_owned(name, forks)

    agents_dir = agent_mod.kiro_agents_dir_path()
    failures: set[str] = set()

    for fork_name in sorted(forks):
        # Owned specs have their own writer; this path must never touch them -- EXCEPT an
        # unconfirmed private copy at the dashboard-author stem, which has no owned writer
        # and must still be governance-filtered here (see _owned_spec_has_its_own_writer).
        if _owned_spec_has_its_own_writer(fork_name, owned_names):
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
                # Read before any pass touches it, so the report below covers a
                # flip by the plumbing refresh as well as by the governed tail.
                merge_was_on = config.get("includeMcpJson") is not False
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
                # Governance passes, the same tail every other spec writer runs:
                # allowedTools and autoApprove are the two paths that never
                # reach the PreToolUse gate, so a fork carrying grants the
                # ceiling later tightened against must be re-filtered on every
                # refresh — this writer is exactly where a stale grant would
                # otherwise persist verbatim. The tail also pins the global
                # ``mcp.json`` merge off, which a fork of a custom template
                # (no plumbing refresh above) would otherwise keep: a server
                # arriving through that merge is outside every filter here. A
                # fork is the user's file, so its KAS block is seeded when
                # absent and never edited, the primary spec's rule. Turning the
                # merge off removes servers the user may rely on, so a flip is
                # logged and audited by fork name rather than made silently.
                agent_state.lift_and_strip_bookkeeping(config, fork_name)
                auto_approve.write_governed_spec(
                    spec_path, config, source=f"fork-refresh:{fork_name}", kas_policy="seed"
                )
                if merge_was_on:
                    _report_mcp_json_merge_pinned_off(fork_name, spec_path)
        except Exception:
            failures.add(fork_name)
            agent_mod.logger.warning(
                "fork refresh failed for %r; its sessions stay blocked", fork_name, exc_info=True
            )
    _fork_refresh_failed = frozenset(failures)
    try:
        from kiro_crew.platform.governance_profiles import governance_answer_generation

        # Read before the pass derives a grant, like the rebuild's own answer.
        generation = governance_answer_generation()
        held_otherwise = _govern_bound_shared_specs(cfg_agents, forks, owned_names)
        # The load fell back to defaults for an unreadable config file, so a crew bound
        # to a template may be missing from cfg_agents entirely.
        config_incomplete = _bindings_may_be_incomplete(cfg)
        if config_incomplete and not held_otherwise:
            from kiro_crew.config.loader import _config_fingerprint

            _shared_template_hold_record = (generation, _config_fingerprint())
    except Exception:
        held_otherwise = True
        config_incomplete = False
        _shared_template_hold_record = None
        agent_mod.logger.warning("shared template governance pass failed", exc_info=True)
    _shared_template_held = held_otherwise or config_incomplete


class SharedTemplateOverCeiling(Exception):
    """A crew-bound spec Kiro Crew does not own carries grants the ceiling denies."""


def require_shared_template_within_ceiling(agent: str | None, backend: str) -> None:
    """Refuse a *backend* start on a crew-bound spec Kiro Crew does not own that the
    ceiling would narrow.

    kiro-cli honours a spec file's ``allowedTools`` and server ``autoApprove`` before
    Kiro Crew's gate, and Kiro Crew never writes a spec it does not own (owner
    decision, docs/decisions). So a session on such a file is started only when the
    two governance passes would remove nothing from any file that claims *agent*:
    otherwise it would auto-approve what the ceiling denies. Only a host in
    ``ACP_BACKENDS_SPEC_FILE_GRANTS`` reads the file's grants, so only its starts
    are checked: every other host gets Crew's projection, which filters or drops them. Bounded like the rebuild pass: only a name
    ``config.json`` binds a crew to, never Kiro Crew's own specs or a fork (their
    writers govern them). A file that cannot be read, or one with a second hard link,
    is refused too: kiro-cli may read what this check could not. Raises
    :class:`SharedTemplateOverCeiling` naming the file and the remedy; reads only.
    """
    from kiro_crew.acp_backends import ACP_BACKENDS_SPEC_FILE_GRANTS

    if not agent or backend not in ACP_BACKENDS_SPEC_FILE_GRANTS:
        return
    from kiro_crew.config.loader import KiroCrewConfig  # circular import

    try:
        from kiro_crew.config.loader import dispatch_kiro_agent

        cfg = KiroCrewConfig.load()
        cfg_agents = cfg.agents
        # A binding can record the file stem (``KiroPkg-captain``) while kiro-cli is
        # dispatched the name that file declares (``captain``), so a binding counts
        # when either spelling is *agent*.
        names = {
            stored
            for stored in (
                getattr(binding, "kiro_agent", None)
                for binding in (cfg_agents.values() if isinstance(cfg_agents, dict) else ())
            )
            if isinstance(stored, str)
            and stored
            and (stored == agent or dispatch_kiro_agent(stored) == agent)
        }
        # A load that could not read a config file whole may be missing the very
        # binding this start runs, so its absence proves nothing: check *agent*'s
        # claimants as if it were bound. So is an agent a crewmate ran on before the
        # startup migration moved it: a conversation from before the move keeps the
        # template its own record names.
        from kiro_crew.mate_config_migration import LedgerUnreadable, moved_from_agents

        try:
            # Recorded as the binding spelled it, so a stem matches under the name
            # its file declares too, the name the old conversation is dispatched.
            moved = {
                m for m in moved_from_agents() if m == agent or dispatch_kiro_agent(m) == agent
            }
            moved_from = bool(moved)
            names |= moved
        except LedgerUnreadable:
            # Not provably never moved, so its claimants are checked; this refuses
            # only a spec the ceiling would narrow, not every start.
            moved_from = True
        if not names and not _bindings_may_be_incomplete(cfg) and not moved_from:
            return
        owned_names = {Path(f).stem for f in OWNED_KIRO_AGENT_FILES}
        over = _claimants_over_ceiling(names | {agent}, agent_state.all_fork_info(), owned_names)
    except Exception as exc:
        # Fail closed: an unread binding or lineage may be the one this start runs.
        raise SharedTemplateOverCeiling(
            f"agent {agent!r}: its spec could not be checked against the governance "
            f"ceiling ({exc}); the session was not started"
        ) from exc
    if over:
        files = ", ".join(str(path) for path in sorted(over)) or "its spec"
        raise SharedTemplateOverCeiling(
            f"agent {agent!r}: {files} grants tools the governance ceiling denies, and "
            "Kiro Crew does not rewrite a spec it does not own. Remove those entries "
            "from its allowedTools or autoApprove, or start the session on KAS, whose "
            "projection filters them."
        )


def _bindings_may_be_incomplete(cfg: object) -> bool:
    """Whether the config load that produced *cfg* could not read a config file whole.

    Asks the load's own read (``_base_unreadable``, ``_overlay_unreadable``), not
    ``degraded_sections``: its whole-config marker stays set for the life of the
    process once either file failed, so a hold on it would never clear after the file
    is repaired and the bindings are complete again.
    """
    return bool(getattr(cfg, "_base_unreadable", False)) or bool(
        getattr(cfg, "_overlay_unreadable", False)
    )


def _scan_agent_specs(agents_dir: Path) -> "tuple[list[tuple[Path, str]], list[Path]]":
    """Read every spec file in *agents_dir* once: the readable ones, and what it could not read.

    Returns each readable file with its text, and every path whose read failed in a way
    that leaves its grants unknown. Reads go through the same fence and capped,
    descriptor-pinned reader the resolver uses: a link, a target outside the directory
    or a sensitive one, and an oversized file are refused for good and never opened.

    Any ``OSError`` on the directory or on a file, a denied read included, is returned,
    so the start check refuses rather than reading the miss as absence: an unreadable
    file may be the one that declares the agent, and a file kiro-cli cannot load while
    its permissions deny the read loads whatever grants it carries once they are
    restored. The listing raises its own errors (:func:`iter_agent_spec_files_strict`)
    rather than yielding an empty directory. A regular file the reader refuses, a second
    hard link above all, is returned too (:func:`_refusal_holds`).
    """
    files, unread = _list_for_pass(agents_dir)
    if unread:
        return [], [agents_dir]
    readable: list[tuple[Path, str]] = []
    failed: list[Path] = []
    for path in files:
        raw, unknown = _read_for_pass(path, agents_dir)
        if raw is not None:
            readable.append((path, raw.decode("utf-8", errors="replace")))
        elif unknown:
            failed.append(path)
    return readable, failed


def _list_for_pass(agents_dir: Path) -> "tuple[list[Path], bool]":
    """The spec files in *agents_dir*, and whether the listing failed.

    A missing directory has no specs. Any other ``OSError`` is a failed listing.
    """
    from kiro_crew.agent_spec_format import iter_agent_spec_files_strict

    try:
        agents_dir.stat()
        return iter_agent_spec_files_strict(agents_dir), False
    except FileNotFoundError:
        return [], False
    except OSError:
        return [], True


def _read_for_pass(path: Path, agents_dir: Path) -> "tuple[bytes | None, bool]":
    """One fenced, capped read of the spec at *path*: ``(bytes, False)`` when it reads,
    ``(None, True)`` when its grants are unknown, ``(None, False)`` when it is no spec.

    No spec: a refused shape (a link, a path outside the directory, a sensitive target),
    a missing, oversized or directory entry, and a refusal of something that is not a
    regular file when checked. Unknown: a refused regular file (:func:`_refusal_holds`)
    and any other ``OSError``, a denied read included.
    """
    from kiro_crew.agent_discovery import _read_spec_bytes, _SpecReadRefused
    from kiro_crew.hooks import FileTooLargeError

    try:
        if agent_mod._spec_path_shape_refusal(path, agents_dir) is not None:
            return None, False
        return _read_spec_bytes(path), False
    except (FileNotFoundError, FileTooLargeError, IsADirectoryError):
        # Windows reports a directory at a spec name from the open, where POSIX
        # reaches the reader's non-regular refusal; neither is a spec, so neither holds.
        return None, False
    except _SpecReadRefused:
        return None, _refusal_holds(path)
    except OSError:
        return None, True


def shared_template_hold_unchanged(generation: int) -> bool:
    """Whether running the pass again now could only end held on the same config read.

    True only when the last pass's hold rests on a config file its load could not read
    whole and nothing else (:data:`_shared_template_hold_record`). *generation* must be
    the governance answer that pass ran under, no pass may be running, and both config
    files must be unchanged and a load still unable to read one whole
    (:func:`_config_still_incomplete`). The rebuild that would run it rewrites
    ``kirocrew.json`` and the owned specs, which kiro-cli watches, so the ceiling hook
    and the maintenance wake skip it on this answer and leave the memo where it is:
    still not projected. A change to the answer or to either config file, or any other
    cause of the hold, makes this False, and that caller rebuilds.
    """
    record = _shared_template_hold_record
    if record is None or not _shared_template_held or not _fork_refresh_settled.is_set():
        return False
    held_generation, config_fingerprint = record
    if held_generation != generation:
        return False
    return _config_still_incomplete(config_fingerprint)


def _config_still_incomplete(fingerprint: tuple) -> bool:
    """Whether neither config file changed and a load still cannot read one of them whole.

    The load is the loader's own: it caches a file whose bytes do not parse under that
    fingerprint, and never caches a read that failed, so a transient read failure that
    cleared reads whole here even though the fingerprint did not move.
    """
    from kiro_crew.config.loader import KiroCrewConfig, _config_fingerprint

    try:
        return _config_fingerprint() == fingerprint and _bindings_may_be_incomplete(
            KiroCrewConfig.load()
        )
    except Exception:
        return False


def _refusal_holds(path: Path) -> bool:
    """Whether a spec the fenced reader refused at *path* still carries unknown grants.

    It does whenever *path* is still a regular file. The usual cause is a second hard
    link: the reader refuses a multiply-linked inode for good, because the other name
    may be any file on the volume, but kiro-cli reads the same bytes without that
    fence, so the grants in it are live and this check cannot read them. A regular file
    refused for another reason (its link count or kernel path changed between the open
    and this check) is one this check could not read either. A WARNING names the file
    and the remedy. A path that is not a regular file at this check (a link, a
    directory, a pipe) is no spec, and one that is gone is absent. A metadata read that
    fails counts as unknown, like a failed read.
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if not stat.S_ISREG(info.st_mode):
        return False
    if info.st_nlink > 1:
        agent_mod.logger.warning(
            "shared template governance: %s has %d hard links, so it cannot be read "
            "safely while kiro-cli still loads it; a kiro-cli start on it is refused "
            "until it is a single-link file (copy it to a new file and remove the "
            "other links)",
            path,
            info.st_nlink,
        )
    else:
        agent_mod.logger.warning(
            "shared template governance: the read of %s was refused; a kiro-cli start "
            "on it is refused until it reads",
            path,
        )
    return True


def _claims(path: Path, text: str, name: str) -> bool:
    """Whether the spec at *path* is one kiro-cli may load as *name*.

    It does when its stem is *name* or it declares ``name: <name>``, and it parses as
    a spec (a JSON object, or markdown with a frontmatter fence): bytes that do not
    parse are no spec kiro-cli can load. Every claimant counts, not one winner. The
    resolvers that act on one agent (``agent.agent_spec_path``) prefer a declared match
    over a stem match and refuse two declared matches, which is right for a writer that
    must not touch a second file; here either rule would leave a claimant's grants
    unfiltered, because which of two claimants is live is undefined.
    """
    from kiro_crew.agent_spec_format import opens_frontmatter_fence

    if is_markdown_spec(path):
        declares = re.compile(rf"^name:\s*['\"]?{re.escape(name)}['\"]?\s*$", re.MULTILINE)
        return opens_frontmatter_fence(text) and (path.stem == name or bool(declares.search(text)))
    try:
        doc = user_json.loads_user_json(text)
    except ValueError:
        return False
    return isinstance(doc, dict) and (path.stem == name or doc.get("name") == name)


def name_has_another_claimant(name: str, path: Path, agents_dir: Path) -> bool:
    """Whether a spec in *agents_dir* other than *path* claims *name*, or may.

    Every claimant counts (:func:`_claims`), and so does the ``<stem>.md`` twin a
    JSON *path* hides, which surfaces once *path* is gone. A file that cannot be read
    may be such a claimant, so it answers True. Reads only.
    """
    from kiro_crew.agent_spec_format import shadowed_markdown_specs

    readable, unread = _scan_agent_specs(agents_dir)
    if unread or any(other != path and _claims(other, text, name) for other, text in readable):
        return True
    return path.with_suffix(".md") in shadowed_markdown_specs(agents_dir)


def _files_with_their_own_writer(
    name: str, forks: "dict[str, dict]", owned_names: "set[str]", agents_dir: Path
) -> "frozenset[Path] | None":
    """The spec files claiming *name* that another writer re-filters, or ``None`` if none does.

    Kiro Crew's installer rewrites only ``<name>.json`` for an owned name it governs
    (:func:`_owned_spec_has_its_own_writer`), and the fork refresh only the file
    ``agent.agent_spec_path`` resolves for a fork. Those files are exempt from the
    shared-template pass, the name is not: any other file that claims it
    (:func:`_claims`) is one kiro-cli may load as that agent, and no writer re-filters
    it but this pass. A fork whose name two files declare has no file the fork refresh
    wrote, so nothing is exempt and every claimant is filtered.
    """
    if _owned_spec_has_its_own_writer(name, owned_names):
        return frozenset({agents_dir / (name + ".json")})
    if name not in forks:
        return None
    try:
        own = agent_mod.agent_spec_path(name)
    except ValueError:
        return frozenset()
    return frozenset() if own is None else frozenset({own})


def _govern_bound_shared_specs(
    cfg_agents: object, forks: "dict[str, dict]", owned_names: "set[str]"
) -> bool:
    """Reconcile the saved capabilities of every crew-bound spec that is not a fork.

    A spec a crew is bound to that is neither Kiro Crew's own nor a fork is a file Kiro
    Crew does not own (a shared template), and nothing here reads or writes it (owner
    decision, docs/decisions): it is also what kiro-cli loads for that agent outside
    Kiro Crew. A kiro-cli start on one the ceiling would narrow is refused instead
    (:func:`require_shared_template_within_ceiling`). What this pass still does is
    the capability reconcile of a bound name with saved capabilities, which writes the
    materialization Kiro Crew owns.

    Returns whether a reconcile raised: a hold no file marks, which the rebuild folds
    into its conductor hold so the ceiling memo does not advance. A reconcile that
    refuses (``CapabilityError``) is deterministic and is warned about, not held.
    """
    agents = cfg_agents if isinstance(cfg_agents, dict) else {}
    bound: dict[str, str] = {}
    for crew, binding in agents.items():
        name = getattr(binding, "kiro_agent", None)
        if isinstance(name, str) and name and isinstance(crew, str):
            bound.setdefault(name, crew)
    held = False
    agents_dir = agent_mod.kiro_agents_dir_path()
    for name in sorted(bound):
        try:
            if _files_with_their_own_writer(name, forks, owned_names, agents_dir) is not None:
                continue
            if agent_state.get_capabilities(name) is None:
                continue
            from kiro_crew.agent_capabilities import (
                CapabilityError,
                reconcile_member_capabilities,
            )

            try:
                reconcile_member_capabilities(bound[name])
            except CapabilityError as exc:
                # A refusal such as a hand-edited materialization: deterministic,
                # so a retry would refuse again.
                agent_mod.logger.warning(
                    "shared template governance: %r was refused by its capability "
                    "reconcile (%s); its grants were not re-filtered against the "
                    "current ceiling",
                    name,
                    exc,
                )
        except Exception:
            held = True
            agent_mod.logger.warning(
                "shared template governance failed for %r", name, exc_info=True
            )
    return held


def _claimants_over_ceiling(
    names: "set[str]", forks: "dict[str, dict]", owned_names: "set[str]"
) -> "frozenset[Path]":
    """Every spec claiming one of *names* that the ceiling would narrow or that cannot be read.

    Reads only. A spec claims a name when its stem is the name or it declares it
    (:func:`_claims`), and every claimant counts, since which one kiro-cli loads is
    undefined. The one file a writer of Kiro Crew's own re-filters is skipped
    (:func:`_files_with_their_own_writer`), and so is a name with saved capabilities,
    whose materialization Kiro Crew owns. A markdown claimant is skipped: kiro-cli does
    not load the markdown form (``ACP_BACKENDS_MARKDOWN_AGENT_SPECS``). A file the
    fenced reader refuses or cannot read, and an agents directory that cannot be listed,
    are returned too (:func:`_scan_agent_specs`): kiro-cli may read what this could not.
    """
    agents_dir = agent_mod.kiro_agents_dir_path()
    over: set[Path] = set()
    scanned: "list[tuple[Path, str]] | None" = None
    for name in sorted(names):
        exempt = _files_with_their_own_writer(name, forks, owned_names, agents_dir)
        if exempt is None and agent_state.get_capabilities(name) is not None:
            continue
        if scanned is None:
            scanned, unread = _scan_agent_specs(agents_dir)
            # kiro-cli never loads the markdown form, so an unread markdown file
            # cannot be the spec it runs; an unlisted directory still counts.
            over.update(p for p in unread if p == agents_dir or not is_markdown_spec(p))
        for spec_path, text in scanned:
            if (exempt is not None and spec_path in exempt) or not _claims(spec_path, text, name):
                continue
            if is_markdown_spec(spec_path):
                continue
            if _spec_exceeds_ceiling(name, spec_path, agents_dir):
                over.add(spec_path)
    return frozenset(over)


def _spec_exceeds_ceiling(name: str, spec_path: Path, agents_dir: Path) -> bool:
    """Would the governance passes narrow the JSON spec at *spec_path*? Reads only.

    True when they would remove something, and when the scan's own read
    (:func:`_read_for_pass`: fenced, capped) refuses or fails, since kiro-cli may
    read what this check could not. Bytes that do not parse as a spec are no spec
    kiro-cli can load, so they carry no grant: False.
    """
    raw, holds = _read_for_pass(spec_path, agents_dir)
    if raw is None:
        return holds
    try:
        config = user_json.loads_user_json(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return False
    if not isinstance(config, dict):
        return False
    before = _spec_grants(config)
    # A check only: nothing is withheld or written, so it leaves no SEL record.
    after = copy.deepcopy(config)
    _apply_governance_passes(after, source=f"shared-template-check:{name}", audit=False)
    # Grants, not documents: a pass that drops an empty list removes no grant.
    return bool(before - _spec_grants(after))


def _spec_grants(config: dict) -> "set[tuple[str, str]]":
    """Every grant in *config* kiro-cli honours before the gate, as comparable pairs.

    ``allowedTools`` entries, and each server's ``autoApprove`` entries. A value that
    is not a list is one grant of its own, so a pass that drops it still counts. An
    absent key and an empty list are the same: no grant.
    """
    grants: set[tuple[str, str]] = set()
    allowed = config.get("allowedTools")
    if isinstance(allowed, list):
        grants.update(("allowedTools", repr(v)) for v in allowed)
    elif allowed is not None:
        grants.add(("allowedTools", repr(allowed)))
    servers = config.get("mcpServers")
    for server, spec in (servers.items() if isinstance(servers, dict) else ()):
        if not isinstance(spec, dict) or "autoApprove" not in spec:
            continue
        asked = spec["autoApprove"]
        if isinstance(asked, list):
            grants.update((f"@{server}", repr(v)) for v in asked)
        elif asked is not None:
            grants.add((f"@{server}", repr(asked)))
    return grants
