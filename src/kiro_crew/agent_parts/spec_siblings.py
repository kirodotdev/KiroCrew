"""The specs a rebuild writes after ``kirocrew.json``, and keeping the kept mirrors governed.

``_install_sibling_specs`` runs every install that follows the main spec: the
service, conductor, heartbeat, worker and dashboard-author specs, the fork refresh
and the hook repair. When the main spec could not be read
(:func:`_skip_unreadable_main_spec`), the heartbeat and worker specs derived from it
are not re-derived, and ``_refilter_kept_mirrors`` instead drops from their existing
grants whatever the current governance ceiling withholds. New work on the
sibling installs or on governing a spec the rebuild could not re-derive goes here.

Composed onto ``kiro_crew.agent``, the import path and patch target of every name
here; see :mod:`kiro_crew.agent_parts`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    # The facade's held-spec flags, which _install_sibling_specs rebinds through
    # ``global`` and never reads; declared for the type checker only.
    _conductor_installers_held: bool
    _conductor_spec_held: bool

    from kiro_crew.agent import (
        _HEARTBEAT_AGENT_FILENAME,
        WORKER_AGENT_FILENAME,
        PlatformCompositionError,
        _atomic_json_write,
        _install_aim_capabilities,
        _install_heartbeat_agent,
        _main_spec_episode_lock,
        _main_spec_unreadable,
        _read_spec_capped,
        _rebuild_spec_install_admitted,
        agents_spec_lock,
        auto_approve,
        conductor_agents,
        first_crewmate,
        fork_refresh,
        kiro_agents_dir_path,
        logger,
        repair_agent_configs,
        service_agents,
        sync_aim_packages,
        worker_agent,
    )


def _install_sibling_specs(
    refresh_forks: bool | Literal["defer"],
    gated_off: frozenset[str],
    *,
    clean: bool,
    mirror_main: bool = True,
) -> bool:
    """The steps of :func:`rebuild_agent_config` that follow the main spec.

    Each writes or re-filters a file other than ``kirocrew.json``, so they run
    whether or not the main spec could be read: skipping them would leave the
    conductor and service specs, the forks and the hook repair on grants an
    earlier ceiling derived. *mirror_main* is False when the main spec could
    not be read. The heartbeat and worker specs are derived from it, and a
    read that failed would rewrite them without the user's servers, so they are
    not re-derived: their existing grants are re-filtered through the current
    ceiling in place instead (:func:`_refilter_kept_mirrors`), so a tightened
    ceiling still reaches them.

    Returns whether a conductor installer left its spec on disk unwritten, the
    verdict the caller hands on through ``_held_out``.
    """
    global _conductor_spec_held, _conductor_installers_held
    # The guard ADMITTED this rebuild above, and that one decision now covers
    # every derived spec it installs. Re-asking per write would answer a
    # different question: ``write_default_spec`` has just created
    # ``kirocrew.json``, and the temp-checkout arm declines precisely when a
    # spec is present -- so each spec below would be refused against the file
    # this rebuild wrote. See ``_rebuild_spec_install_admitted``.
    #
    # ``finally``, not a plain reset after the block: several of these calls can
    # raise past their own arms, and a leaked flag would exempt every later
    # write in the process -- a worker dispatch, a dashboard edit -- from the
    # spec-install guard.
    _rebuild_admitted_token = _rebuild_spec_install_admitted.set(True)
    try:
        # A ``PlatformCompositionError`` from any installer below is the fail-closed
        # signal that this host cannot compose its companion; it must abort the rebuild
        # (``safe_context_call``'s contract: it is never swallowed). It is CAPTURED here
        # rather than raised in place so the independent, companion-agnostic repairs at
        # the end of this block -- ``repair_agent_configs`` hook-key sanitization and the
        # fork refresh -- still run, exactly as a failed dashboard-author install is
        # captured and re-raised at the end for the same reason. The first one wins; it is
        # raised after the ``finally``, before ``_wrote_out`` is marked, so the rebuild
        # reports no write.
        composition_error: PlatformCompositionError | None = None

        # Install Kiro Crew AIM capabilities package (includes kirocrew-lite)
        _install_aim_capabilities()

        # Install kirocrew-knowledge agent (used by Knowledge Library LLMPool)
        try:
            service_agents._install_knowledge_agent()
        except PlatformCompositionError as exc:
            composition_error = composition_error or exc
        except Exception:
            logger.debug("kirocrew-knowledge agent install failed", exc_info=True)

        # Tracks a failed research rewrite the same way ``team_lead_held`` below tracks
        # team-lead. ``_install_research_agent`` derives from ``build_agent_config()``,
        # so its ``allowedTools`` is re-filtered through the governance ceiling on every
        # boot -- a ceiling-TIGHTENING rebuild whose research rewrite failed leaves the
        # previous list, with the now-revoked grants, on disk. Swallowing that at debug
        # would let ``reproject_for_ceiling_change`` advance its generation memo so the
        # next poll would NOT retry, and the stale grants would persist for the process
        # lifetime (a ceiling-filtered installer must not be recorded as synced over an
        # unrewritten grant list). Folded into ``conductor_held`` beside ``team_lead_held``
        # below.
        research_held = False

        # Install kirocrew-research agent (used by the Research Lab campaign loop)
        try:
            service_agents._install_research_agent()
        except PlatformCompositionError as exc:
            composition_error = composition_error or exc
        except Exception:
            research_held = True
            logger.warning(
                "kirocrew-research agent install failed; marking the ceiling held so a "
                "tightened ceiling is retried rather than recorded as synced over the "
                "stale grant list left on disk",
                exc_info=True,
            )

        # Tracks a failed mate-conductor rewrite the way ``research_held`` and
        # ``team_lead_held`` do: ``_install_mate_conductor_agent`` also derives
        # from ``build_agent_config()``, so a failed rewrite under a tightening
        # ceiling must hold the memo rather than let it advance over a stale
        # grant list on disk. Folded into ``conductor_held`` below.
        mate_conductor_held = False

        # Install kirocrew-mate-conductor agent (the agent a crewmate's own chat
        # runs on) and kirocrew-mate-worker (what its worker sessions run on).
        try:
            service_agents._install_mate_conductor_agent()
            service_agents._install_mate_worker_agent()
        except PlatformCompositionError as exc:
            composition_error = composition_error or exc
        except Exception:
            mate_conductor_held = True
            logger.warning(
                "kirocrew-mate-conductor agent install failed; marking the ceiling held so "
                "a tightened ceiling is retried rather than recorded as synced over the "
                "stale grant list left on disk",
                exc_info=True,
            )

        # Tracks a failed team-lead rewrite the way ``conductor_held`` tracks the
        # conductor specs. It is folded into ``_conductor_spec_held`` AFTER that flag
        # is settled below, because the settle is a plain assignment that would
        # otherwise clobber a hold set here (this installer runs before it).
        team_lead_held = False

        # Install kirocrew-team-lead agent (a default agent an operator selects like
        # any other). Its ``allowedTools`` is re-filtered through the governance
        # ceiling on every boot, exactly like the conductor specs above -- it derives
        # from ``build_agent_config()`` and passes its added dispatch grants through
        # the same ceiling filter before the write.
        # So a ceiling-TIGHTENING rebuild whose team-lead rewrite failed (a read-only
        # file, a refused ``os.replace``, a spec-lock timeout) leaves the previous
        # list -- with the now-revoked grants -- on disk. Swallowing that at debug
        # would let ``reproject_for_ceiling_change`` advance its generation memo and
        # the next poll would NOT retry, so the stale grants would persist for the
        # process lifetime. Mark the hold instead, like the conductor
        # specs and the dashboard-author below: ``prime_ceiling_projection`` then does
        # not seed the ceiling as projected, and ``retry_held_conductor_specs`` retries
        # the rewrite on the next maintenance poll. With the spec simply absent the
        # lead is not offered and nothing else depends on it; the hold is only about
        # not recording a tightened ceiling as synced over an unrewritten grant list.
        try:
            service_agents._install_team_lead_agent()
        except PlatformCompositionError as exc:
            composition_error = composition_error or exc
        except Exception:
            team_lead_held = True
            logger.warning(
                "kirocrew-team-lead agent install failed; marking the ceiling held so a "
                "tightened ceiling is retried rather than recorded as synced over the "
                "stale grant list left on disk",
                exc_info=True,
            )

        # Install kirocrew-dashboard-manager agent (the subagent a crewmate hands page
        # work to). Degrades to a debug line like the two above: with the spec absent a
        # crewmate still has `dashboard_fields` and `dashboard_write`, so the page it has
        # keeps working and only CHANGING the page is unavailable.
        try:
            service_agents._install_dashboard_manager_agent()
        except PlatformCompositionError as exc:
            composition_error = composition_error or exc
        except Exception:
            logger.debug("kirocrew-dashboard-manager agent install failed", exc_info=True)
        # Every install, fresh or upgraded, creates its first crewmate once (a
        # one-time marker, so a deleted one is never re-created). The reserved
        # ``default`` member is never touched. Boot keeps going either way.
        try:
            first_crewmate.create_first_crewmate_once()
        except Exception:
            logger.warning("First Bot creation failed", exc_info=True)

        # Install kirocrew-heartbeat agent (used by HeartbeatService for unattended polling)
        # mirrors_held: the in-place re-filter of the kept heartbeat and worker specs
        # failed, so a tightened ceiling may not have reached them. Folded into
        # ``conductor_held`` below so the memo stays behind and the retry runs again.
        mirrors_held = False
        if mirror_main:
            try:
                _install_heartbeat_agent()
            except PlatformCompositionError as exc:
                composition_error = composition_error or exc
            except Exception:
                logger.debug("kirocrew-heartbeat agent install failed", exc_info=True)
        else:
            mirrors_held = not _refilter_kept_mirrors()

        # Install kirocrew-conductor agent (goal decomposition + session-control dispatch).
        # ``clean`` is passed through: the conductor installers carry the user's own
        # ``allowedTools`` entries forward across a rebuild, and a clean rebuild is the
        # explicit reset that drops them, as it drops every customization above. Each
        # reports whether it wrote: a spec left on disk because it could not be read
        # is a list this rebuild did NOT re-derive, and so is one whose installer
        # raised -- a failed write (a read-only file, a refused ``os.replace``) leaves
        # the previous list on disk exactly as a refused read does -- and ``_held_out``
        # must say so in both cases, or a moved ceiling is marked projected onto a
        # list that still carries the old grants.
        conductor_held = False
        try:
            conductor_held |= not conductor_agents._install_conductor_agent(clean=clean)
        except PlatformCompositionError as exc:
            composition_error = composition_error or exc
        except Exception:
            conductor_held = True
            logger.debug("kirocrew-conductor agent install failed", exc_info=True)

        # Install kirocrew-pipeline-conductor agent (repository pipeline fleet supervision)
        try:
            conductor_held |= not conductor_agents._install_pipeline_conductor_agent(clean=clean)
        except PlatformCompositionError as exc:
            composition_error = composition_error or exc
        except Exception:
            conductor_held = True
            logger.debug("kirocrew-pipeline-conductor agent install failed", exc_info=True)

        # Install the deprecated kirocrew-ledger-conductor alias (the same spec as
        # kirocrew-conductor above, under its old name, for one release). EAGER for
        # the same forced reason spelled out on the worker below: ``session_create``
        # refuses an agent it cannot resolve, and resolution reads a boot-time
        # in-memory snapshot that no spec write refreshes — so a lazily-materialized
        # spec is invisible to the validation that runs ahead of the spawn. A session
        # already running under the old name resolves it on every dispatch, which is
        # what the alias exists to keep working.
        try:
            conductor_held |= not conductor_agents._install_ledger_conductor_agent(clean=clean)
        except PlatformCompositionError as exc:
            composition_error = composition_error or exc
        except Exception:
            conductor_held = True
            logger.debug("kirocrew-ledger-conductor alias install failed", exc_info=True)

        # Install kirocrew-security-conductor agent (one security audit's worker fleet)
        try:
            conductor_held |= not conductor_agents._install_security_conductor_agent(clean=clean)
        except PlatformCompositionError as exc:
            composition_error = composition_error or exc
        except Exception:
            conductor_held = True
            logger.debug("kirocrew-security-conductor agent install failed", exc_info=True)
        # Settled here, not at the end: ``prime_ceiling_projection`` seeds the ceiling
        # baseline from this after the boot rebuild, and a rebuild that raises further
        # down must still leave the installers' verdict behind, not the previous one.
        # Never cleared while an unreadable-spec episode is open: an overlapping
        # rebuild may have skipped since this one wrote the main spec, and its hold
        # is what keeps the retry running. Read and written under the lock the skip
        # takes, so the two flags are settled together.
        # ``team_lead_held`` folds in here (not only into ``_conductor_spec_held``)
        # because the end-of-rebuild ``_held_out.append`` reads ``conductor_held``.
        # ``research_held`` and ``mate_conductor_held`` fold in for the same reason:
        # all three derive from ``build_agent_config`` and must not be recorded
        # synced over a stale grant list.
        conductor_held = (
            conductor_held or team_lead_held or research_held or mate_conductor_held or mirrors_held
        )
        with _main_spec_episode_lock:
            _conductor_spec_held = conductor_held or _main_spec_unreadable
            _conductor_installers_held = conductor_held

        # Install kirocrew-dashboard-author agent (authors one dashboard template and lands
        # it as a PR). EAGER for the same forced reason spelled out on the worker above:
        # ``session_create`` refuses an agent it cannot resolve, resolution reads a boot-time
        # in-memory snapshot that no spec write refreshes, so a lazily-materialized spec is
        # invisible to the validation that runs ahead of the spawn -- a conductor could never
        # dispatch it on a clean install. Being here also re-filters its grants through the
        # governance ceiling on every boot, so the spec normally cannot outlive a tightened
        # ceiling -- with one exception: a file at the stem that is NOT this installer's own
        # (its bytes do not reproduce the installer-recorded ownership digest) is left untouched,
        # so a hand-authored user spec at the once-user-creatable stem -- or a copy of another
        # owned agent renamed onto it -- is never overwritten. Ownership is the digest the
        # installer records in the ``agent_state`` sidecar for this name.
        dashboard_author_install_error: Exception | None = None
        try:
            worker_agent._install_dashboard_author_agent()
        except PlatformCompositionError as exc:
            # The fail-closed signal routes to the shared slot like every other installer,
            # so it is raised at the end (after the independent repairs) and takes priority
            # over an ordinary author-install failure below.
            composition_error = composition_error or exc
        except Exception as exc:
            # Capture, do NOT swallow. An installer failure must not ABORT the independent
            # repairs below (fork governance refresh, hook repair) -- those run for every other
            # installer's failure too -- so we let them run first. But it must not be reported as
            # a SUCCESSFUL rebuild either: this spec's ``allowedTools`` is re-filtered through the
            # governance ceiling on every rebuild, so a ceiling-TIGHTENING rebuild whose
            # author-spec rewrite failed (e.g. a Windows sharing violation, a spec-lock timeout)
            # leaves the spec's forbidden auto-approvals live. If this rebuild then reported
            # success, ``reproject_for_ceiling_change`` would advance its generation memo and the
            # next poll would NOT retry -- the forbidden grants would persist for the process
            # lifetime. So the failure is re-raised at the END of the rebuild (after the
            # independent repairs), which (a) escapes before ``_wrote_out`` is marked True, so
            # ``rebuild_agent_config_reporting`` does not report a write, and (b) leaves the
            # reprojection memo behind so the next poll retries -- the safe direction the
            # success contract already documents for a post-write exception.
            dashboard_author_install_error = exc
            # A tightened ceiling whose author-spec rewrite failed leaves revoked auto-
            # approvals live. Mark the hold so ``prime_ceiling_projection`` does not seed
            # the ceiling as projected, and ``retry_held_conductor_specs`` retries the
            # rewrite on the next maintenance poll; without the hold a boot-time author
            # install failure loses the governance retry.
            with _main_spec_episode_lock:
                _conductor_spec_held = True
                _conductor_installers_held = True
            logger.warning(
                "kirocrew-dashboard-author agent install failed; completing independent repairs "
                "then failing the rebuild so a tightened ceiling is retried, not marked synced",
                exc_info=True,
            )

        # Install kirocrew-worker agent (the default toolset plus the work-ledger set).
        #
        # EAGER, like its six siblings above, and that placement is forced rather than
        # chosen. ``session_create`` refuses an agent it cannot resolve
        # (``agent_unresolved``), resolution runs through
        # ``config.loader._materialized_kiro_agent``, and that is a pure IN-MEMORY
        # snapshot refreshed at boot and by app (de)registration — never by a spec
        # write. A spec materialized on the spawn path is therefore invisible to the
        # validation that runs ahead of the spawn, so on a clean install a conductor
        # cannot dispatch a worker at all. Measured: the name resolves False in the boot
        # snapshot, and still False after a lazy write until a refresh nothing triggers.
        #
        # Being here also means every boot re-filters this spec's grants through the
        # governance ceiling, exactly as it does for the six siblings, so the spec
        # cannot outlive a tightened ceiling.
        if mirror_main:
            try:
                worker_agent._install_worker_agent()
            except PlatformCompositionError as exc:
                composition_error = composition_error or exc
            except Exception:
                logger.debug("kirocrew-worker agent install failed", exc_info=True)

        # Bidirectional sync: ensure packages installed for one provider
        # are also available for the other (agents↔plugins, skills).
        sync_aim_packages()

        fork_refresh.refresh_after_rebuild(refresh_forks, gated_off)
        if refresh_forks is True and fork_refresh._shared_template_held:
            # A crew-bound shared template kept grants the ceiling may now deny
            # because its rewrite failed. Held like a conductor spec, so the
            # ceiling memo stays behind and the next poll or wake retries.
            conductor_held = True
            with _main_spec_episode_lock:
                _conductor_spec_held = True

        # Security: sanitize invalid hook keys in agent configs
        repair_agent_configs()
    finally:
        _rebuild_spec_install_admitted.reset(_rebuild_admitted_token)

    if composition_error is not None:
        # The independent repairs have run; now fail the rebuild with the fail-closed
        # signal. ``_wrote_out`` is left UNMARKED (the reporting wrapper never appends
        # True), so a host that cannot compose its companion does not report a rebuild it
        # could not safely complete. It takes priority over a plain author-install failure.
        raise composition_error

    if dashboard_author_install_error is not None:
        # The independent repairs have run; now fail the rebuild. ``_wrote_out`` is left
        # UNMARKED (so the reporting wrapper returns ``wrote=False``-equivalent by never
        # appending True) and the exception propagates, which is what keeps a tightened
        # ceiling from being recorded as projected while the governed spec was not rewritten.
        raise dashboard_author_install_error
    return conductor_held


def _refilter_kept_mirrors() -> bool:
    """Drop what the current ceiling forbids from the kept heartbeat and worker specs.

    Runs in place of their installers while ``kirocrew.json`` cannot be read. The
    installers derive both specs from the main spec, so they cannot run, but each
    rebuild still owes the grants already on disk the current ceiling: kiro-cli
    auto-approves ``allowedTools`` and a server's ``autoApprove`` before the
    PreToolUse gate sees the call. Each spec is read and run through
    ``fork_refresh._apply_governance_passes``, the one owner of the two passes the
    installers run: ``allowedTools`` goes through the ceiling and every server loses
    an ungoverned ``autoApprove``. A worker spec that carries a KAS
    ``permissions`` block has it derived again from the filtered list, as its
    installer does. Nothing is added: no server is copied from the main spec, which
    cannot be read, and a grant the ceiling still allows stays. The spec is written
    back only when a pass removed something. Each read-filter-write runs under the
    locks its installer takes.

    A missing spec is skipped, and so is a worker file another writer owns
    (``worker_agent._foreign_worker_spec_reason``), which its installer would refuse
    to overwrite too. Returns False when a spec exists but could not be read or
    written, so the caller holds the ceiling memo and the next rebuild retries.
    """
    from kiro_crew.apps.bridges import _mcp_lock  # noqa: PLC0415 - boot path

    agents_dir = kiro_agents_dir_path()
    ok = True
    for name in (_HEARTBEAT_AGENT_FILENAME, WORKER_AGENT_FILENAME):
        path = agents_dir / name
        try:
            with agents_spec_lock(agents_dir), _mcp_lock():
                if not path.exists():
                    continue
                config = _read_spec_capped(path)
                if not isinstance(config, dict):
                    raise ValueError(f"{path} could not be read as an agent spec")
                if (
                    name == WORKER_AGENT_FILENAME
                    and worker_agent._foreign_worker_spec_reason(config) is not None
                ):
                    continue
                # A deep copy through JSON: the spec came from JSON, and the
                # comparison below must see every nested change.
                governed = json.loads(json.dumps(config))
                fork_refresh._apply_governance_passes(governed, source="_refilter_kept_mirrors")
                if "permissions" in governed:
                    auto_approve._write_derived_permissions(
                        governed, governed.get("allowedTools") or [], name
                    )
                if governed != config:
                    _atomic_json_write(path, governed)
                    logger.info(
                        "re-filtered %s through the current governance ceiling; the main "
                        "agent spec could not be read, so it was not re-derived",
                        path,
                    )
        except Exception:
            ok = False
            logger.warning(
                "could not re-filter %s through the current governance ceiling; the "
                "next rebuild retries",
                path,
                exc_info=True,
            )
    return ok
