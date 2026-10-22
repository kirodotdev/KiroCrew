"""Declining an agents home another install owns.

The audit of a write into a shared home, the provenance check over the specs already in
one, and the guard that keeps an ephemeral instance (a linked worktree, a temporary
tree) from rewriting the specs of the install it shares a home with. New work on who may
write a shared home goes here.

Composed onto ``kiro_crew.agent``, the import path and patch target of every name
here; see :mod:`kiro_crew.agent_parts`.
"""

from __future__ import annotations

import contextvars
import json
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.agent import (
        _DASHBOARD_AUTHOR_FILENAME,
        _MANAGED_MCP_SERVERS,
        AGENT_FILENAME,
        OWNED_KIRO_AGENT_FILES,
        FileTooLargeError,
        _in_ephemeral_tree,
        _in_linked_git_worktree,
        _spec_path_is_safe,
        _under_system_tmp,
        _valid_override_home,
        ambient_agents_dir,
        isolated_agents_dir,
        kiro_agents_dir_path,
        logger,
        safe_read_file_bytes_nolink,
        sel,
        shared_kiro_agents_writable,
    )


def _audit_allowed_shared_write(path: Path) -> None:
    """Record the "allowed" half of an admitted derived-spec write, best-effort.

    Called only from :func:`_declined_foreign_spec_write` when the write is
    admitted (``_decline_shared_agent_home`` returned ``None``) and NOT inside an
    already-admitted rebuild. It closes the audit gap a per-dispatch writer leaves
    when it is admitted OUTSIDE the boot rebuild -- the worker mirror re-derived
    on spawn after an app deregistration left it stale is the motivating case --
    so that write carries an ``agent_home_write`` / ``allowed`` event under its
    own source, matching the ``denied`` half this guard already records.

    EVERY admitted shared-home write is recorded, including the default home.
    This path is reached with ``audit=False`` passed to the shared decision, so
    the decision itself emits nothing -- without this call the permission grant
    for an admitted write made outside a rebuild would be unaudited, which is a
    gap in the trail whether or not the home is the default one. The rebuild
    already audits its own grant once per rebuild and does not reach here (the
    ``_rebuild_spec_install_admitted`` arm returns earlier), so there is no
    double-record.

    A PRIVATE target is the one case that stays silent: ``_decline_shared_agent_home``
    returns ``None`` for a redirected target the ambient environment would never
    produce (a test ``tmp_path``, a relocated agents dir), and that is not a
    decision about the shared resource -- the same reason that function does not
    audit its private-target returns. The target is the shared one only when the
    configured agents dir resolves to the ambient one; otherwise it is a private
    redirect and we emit nothing.

    Best-effort: any failure (an unresolvable path, an unavailable audit sink) is
    swallowed, because an allowed write must never be turned into a failure by a
    lost record -- the same contract the ``denied`` branch keeps.
    """
    try:
        if kiro_agents_dir_path().resolve() != ambient_agents_dir().resolve():
            # A private redirect, not the shared directory: no shared-resource
            # decision was made here, so (like _decline_shared_agent_home's own
            # private-target returns) emit nothing.
            return
        sel().log_api_access(
            caller="system",
            operation="agent_home_write",
            outcome="allowed",
            source="derived-spec",
            resources=str(path.parent),
        )
    except Exception:  # noqa: BLE001 — a lost record must not turn an allowed write into a failure
        logger.debug("SEL audit unavailable for an allowed shared write", exc_info=True)


#: Bounds for the shared-home ownership probe's spec reads. Per-spec cap is
#: ~24x the largest real owned spec (~11 KB measured); the total budget bounds
#: the whole loop even if OWNED_KIRO_AGENT_FILES grows. Sized so no legitimate
#: spec is ever near them while a pathological file cannot occupy the event
#: loop the boot-path rebuild runs on. Over-bound reads REFUSE, never parse.
_PROVENANCE_SPEC_CAP_BYTES = 256 * 1024


_PROVENANCE_TOTAL_BUDGET_BYTES = 2 * 1024 * 1024


def _existing_specs_are_mine(target: Path, own_home: Path | None) -> bool | None:
    """Do the owned specs already in *target* record THIS instance as writer?

    The specs carry their own provenance: ``rebuild_agent_config`` pins the
    writing instance's ``KIROCREW_HOME`` into every managed server entry
    (``_managed_mcp_env``), and a default-home writer pins nothing. Reading
    that back is what lets the shared-home guard refuse FOREIGN specs instead
    of ALL specs — a self-pinned spec is this instance's own, so its later
    rebuilds keep refreshing it rather than locking themselves out.

    Only entries named in ``_MANAGED_MCP_SERVERS`` are consulted — custom
    entries flow in from user configuration and cannot vouch — and EVERY
    managed entry present must record *own_home*: the writer pins them all in
    one rebuild, so a spec whose managed entries disagree (one pinned here,
    one pinned elsewhere or not at all) was not written whole by this
    instance and is foreign. A spec with no managed entry at all (the lite
    and knowledge agents) carries no signature and is neutral rather than
    foreign, so a complete self-written set reads back as its writer's.

    Returns ``None`` when no owned spec exists (nothing to preserve — a fresh
    write is safe), ``True`` when the specs that carry a signature all record
    *own_home*, and ``False`` otherwise — including unreadable or malformed
    specs and a set where only neutral siblings survive (ownership
    unknowable). Every ``False`` shape refuses, the conservative direction
    the capped reader already takes for anything it cannot parse.

    **The recorded value is compared, never interpreted.** ``recorded`` is
    spec content — untrusted by this function's own charter — so it performs
    ZERO filesystem operations on it: no ``expanduser``, no ``resolve``, no
    ``stat``. On Windows, resolving an attacker-planted UNC value
    (``\\\\host\\share\\...``) fires outbound SMB authentication to that host;
    lexical comparison cannot. The writer's pin is produced already-resolved
    (``_managed_mcp_env`` pins ``str(_valid_override_home())``), so string
    equality after ``os.path.normpath(...)`` — a pure string function, no OS
    access — is exact against every pin this code ever writes. The comparison
    is case-PRESERVING on purpose: ``normcase`` would fold ``C:\\Crew`` and
    ``C:\\crew`` together, reading a distinct case-sensitive home's spec as
    this instance's own — a fail-open. A pin that is merely
    resolution-equivalent (a retargeted symlink, a moved home, a parent
    directory that later becomes a symlink) reads foreign and refuses, which
    is the conservative direction above and cannot lock out a self-written
    install whose resolved home is stable: its pin came from the same
    resolver the reader's *own_home* did. The spec files themselves are
    admitted through the same :func:`_spec_path_is_safe` fence every other
    reader in ``kiro_crew.agent`` applies — a symlinked or out-of-directory spec is
    present-and-unvouchable, never followed.
    """
    found = False
    signals = 0
    expected = None if own_home is None else os.path.normpath(str(own_home))
    # The ownership probe is bounded ON THE DESCRIPTOR IT READS: the bytes
    # come through safe_read_file_bytes_nolink with max_bytes pinned to
    # _PROVENANCE_SPEC_CAP_BYTES and the remaining loop budget, so a spec
    # that grows after any earlier check still cannot exceed the cap — the
    # reader opens O_NOFOLLOW, fstats THAT inode, rejects non-regular and
    # hardlinked files, and refuses past the limit. The lstat pre-check
    # rejects non-regular files (a FIFO would otherwise park the open) and
    # over-cap sizes before any open. Residual, stated honestly: a same-uid
    # writer racing the lstat->open window can swap in a FIFO and park the
    # open — that writer already owns the account and this directory, the
    # same inherited class as every other spec read here. The rebuild can
    # run on the event loop (the gateway's boot path calls it directly), so
    # these bounds are what keep a pathological "spec" from occupying it;
    # the largest real owned spec is ~11 KB, nowhere near either bound. An
    # over-bound spec refuses (False), the conservative direction every
    # unparseable shape already takes.
    budget = _PROVENANCE_TOTAL_BUDGET_BYTES
    for name in OWNED_KIRO_AGENT_FILES:
        spec_path = target / name
        # No-follow presence probe: exists() would FOLLOW a planted symlink,
        # and a dangling one would then read as "no spec here" — an absence
        # verdict an attacker can manufacture in the same-uid agents dir.
        if not os.path.lexists(spec_path):
            continue
        # The dashboard-author stem was a user-creatable template name before it became
        # owned, and its managed ``mcpServers`` entry carries no ``KIROCREW_HOME`` pin on
        # either branch -- the installer's own write omits it, and a leftover user file at
        # the stem never had one. So this stem can never contribute a legitimate home-pin
        # signal, and letting it into the probe makes a non-default home read every such
        # file (ours OR an untouched user leftover) as foreign and refuse creation of every
        # required spec. Exclude it from the pin probe regardless of its provenance; its
        # provenance is the installer-recorded ownership digest, checked at the install gate,
        # not a home pin.
        if name == _DASHBOARD_AUTHOR_FILENAME:
            continue
        found = True
        if expected is None:
            return False
        if not _spec_path_is_safe(spec_path, target):
            # Present but unvouchable (a symlink, an out-of-dir resolution):
            # refuse rather than read through it.
            return False
        try:
            st = spec_path.lstat()
        except OSError:
            return False
        if not stat.S_ISREG(st.st_mode):
            return False
        if st.st_size > _PROVENANCE_SPEC_CAP_BYTES or st.st_size > budget:
            return False
        try:
            raw = safe_read_file_bytes_nolink(
                str(spec_path),
                within_root=str(target),
                max_bytes=min(_PROVENANCE_SPEC_CAP_BYTES, budget),
            )
        except FileTooLargeError:
            # The file grew between the lstat and the descriptor read. The
            # reader stopped at the bound, so memory stayed capped — refuse,
            # never abort the rebuild over a spec that changed underneath us.
            return False
        if raw is None:
            return False
        budget -= len(raw)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return False
        if not isinstance(parsed, dict):
            return False
        data = parsed
        servers = data.get("mcpServers")
        has_managed_entry = False
        if isinstance(servers, dict):
            for server_name in _MANAGED_MCP_SERVERS:
                server_spec = servers.get(server_name)
                if not isinstance(server_spec, dict):
                    continue
                has_managed_entry = True
                env = server_spec.get("env")
                recorded = env.get("KIROCREW_HOME") if isinstance(env, dict) else None
                if not isinstance(recorded, str) or "\x00" in recorded:
                    # A NUL can raise from the C normpath on some platforms; a
                    # provenance read refuses garbage, never aborts setup.
                    return False
                if os.path.normpath(recorded) != expected:
                    return False
        if has_managed_entry:
            signals += 1
    if not found:
        return None
    return signals > 0


#: (target, arm) pairs whose shared-home refusal already logged at WARNING.
#: A structurally-declined instance (a linked worktree, a pod, a relocated
#: home beside foreign specs) re-attempts the rebuild every refresh poll and
#: refuses every time, so an unthrottled WARNING repeats ~1/min for the
#: process lifetime. The SEL denied event is deliberately NOT throttled —
#: every permission decision over the shared resource stays in the audit
#: trail — only the operator-facing log line drops to debug on repeats,
#: mirroring session_pid_sig's _report_signing_unavailable.
_declined_home_warned: set[tuple[str, str]] = set()


#: Set only while a ``rebuild_agent_config`` call that the shared-home guard
#: ALREADY ADMITTED is still installing its specs. Read by
#: :func:`_declined_foreign_spec_write` so the derived writes inside that
#: rebuild are covered by the rebuild's own decision instead of re-deriving one
#: per write.
#:
#: The re-derivation is not merely wasteful, it inverts: the temp-checkout arm
#: declines only when a spec is present to preserve, and the rebuild's own first
#: write creates one. So a fresh install from a plain temp checkout wrote
#: ``kirocrew.json``, and every spec after it in the SAME rebuild -- knowledge,
#: research, heartbeat, the conductors, the worker mirror -- was then refused
#: against the file the rebuild had just created. A dispatch afterwards fails
#: with ``agent_unresolved``.
#:
#: This cannot widen the guard, because it is only ever set on the ALLOWED path:
#: a rebuild the guard declines returns before reaching the flag and writes
#: nothing. Reset via its token in a ``finally``, so it never outlives its own
#: call and never leaks into a write made outside one -- a worker dispatch, a
#: dashboard edit and the Connections mint all still ask the guard for
#: themselves, which is the whole point of asking it at the primitive.
#:
#: A ``ContextVar``, not a module global, so the exemption is scoped to the
#: rebuild's OWN call stack rather than the whole process. ``rebuild_agent_config``
#: is synchronous and the dashboard/dispatch writers reach the guard on worker
#: threads via ``asyncio.to_thread``; each ``to_thread`` call runs the target in
#: a COPY of the context taken when it was submitted, so a value set inside the
#: rebuild's execution is confined to the rebuild's context and is NOT observed
#: by a concurrent threaded writer -- that writer sees the default and consults
#: the guard for itself, even if a config-watcher rebuild is in flight.
_rebuild_spec_install_admitted: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_rebuild_spec_install_admitted", default=False
)


def _warn_declined_home_once(arm: str, target: Path, msg: str, *args: object) -> None:
    """WARN the first shared-home refusal per (target, arm); debug thereafter."""
    key = (str(target), arm)
    if key in _declined_home_warned:
        logger.debug(msg, *args)
        return
    _declined_home_warned.add(key)
    logger.warning(msg, *args)


def _decline_shared_agent_home(*, audit: bool = True) -> Path | None:
    """Return the spec path to report, WITHOUT writing, when this instance must
    not own the shared agent home; ``None`` when writing is safe.

    An **ephemeral** Kiro Crew instance — one booted from a linked git worktree, or
    one running on its own isolated ``KIROCREW_HOME`` (a pod) — is throwaway by
    construction, but the agent specs it writes are not. ``rebuild_agent_config``
    stamps this instance's own ``.venv`` binary into every managed server's
    ``command`` and its own data home into their ``env``. Written into a spec the
    REAL install also reads, that makes the live gateway's MCP servers run this
    tree's code and read this instance's ``.local_secret`` while still calling the
    live gateway — every managed MCP request 403s (``learn_add``, ``spawn_run``,
    ``cron_*`` all die) — and tearing the instance down leaves those shared specs
    pointing at paths that are gone.

    Declining mirrors ``ensure_kirocrew_on_path``'s worktree guard: leave whatever
    already worked in place rather than repointing a shared resource at an
    ephemeral one.

    The predicate is deliberately **"is the target shared, and am I ephemeral"** —
    NOT "am I in a worktree", and NOT "is the target the hard-coded
    ``~/.kiro/agents``". A second, independent arm refuses the shared target from
    any instance whose data home is not the default one, ephemeral or durable
    (:func:`shared_kiro_agents_writable`) — see the inline comment at that
    arm. Two bypasses of those narrower forms are closed here:

    * A **pod running from the primary checkout** is not in a linked worktree at
      all, yet ``pod down`` deletes its home and checkout venv, so it still leaves
      the machine-wide specs dangling. Pods therefore declare themselves via
      ``KIROCREW_POD`` (set in ``build_pod_env``) and that counts as ephemeral on
      its own. Note what is deliberately NOT used as the signal: merely *having*
      an isolated ``KIROCREW_HOME``. A CI test gateway (the offline E2E suite boots
      on a tmp data home) and a user who permanently relocated their data home are
      indistinguishable from a pod under that rule, and stopping either from
      writing its specs is a regression, not protection.
    * A globally exported ``KIRO_HOME`` moves the shared directory, so comparing
      against a hard-coded default reads "not the shared one" and waves the write
      straight through. The comparison is therefore against what the AMBIENT
      environment resolves right now (``ambient_agents_dir()``), which is by
      definition the directory every instance under this environment shares.
      Deliberately the override-BLIND resolver, not ``kiro_agents_dir()``: the
      latter follows ``config.paths._agents_dir_override``, so a redirect would
      move both sides of this comparison together, read as "target is the shared
      one", and refuse the write from any ephemeral checkout.

    A target is exempt only when it is **provably private**: either a caller
    redirected the write somewhere the ambient environment would never produce (a
    test's ``tmp_path``), or it is EXACTLY ``isolated_agents_dir(own data home)``
    — the dedicated ``<data home>/kiro/agents`` this instance's teardown owns.

    That second case is the *mechanism* by which a genuinely isolated instance will
    own its specs; it is NOT advice to set ``KIRO_HOME`` by hand. A pod sets it
    (``build_pod_env`` passes ``KIRO_HOME=<pod home>/kiro``), but setting it also
    relocates kiro-cli's session storage while Kiro Crew still reads the host path
    — see ``kiro_home()``'s scope caveat. The exemption is matched exactly rather
    than by ancestry: "beneath the data home" reads the machine-wide
    ``~/.kiro/agents`` as private the moment the data home is an ancestor of it
    (``KIROCREW_HOME=$HOME`` is enough).
    """
    target = kiro_agents_dir_path().resolve()
    if target != ambient_agents_dir().resolve():
        # A caller pointed the write somewhere of its own choosing; nothing is
        # shared with the ambient install, so there is nothing to protect.
        return None

    own_home = _valid_override_home()
    if own_home is not None and target == isolated_agents_dir(own_home).resolve():
        # The one supported opt-in: the DEDICATED agents dir beneath this
        # instance's own data home, which its teardown owns. Matched exactly, not
        # by ancestry — "anywhere beneath the data home" reads the machine-wide
        # ~/.kiro/agents as private whenever the data home is an ancestor of it
        # (KIROCREW_HOME=$HOME suffices), handing an ephemeral instance the very
        # specs this guard protects. A different KIRO_HOME layout is refused
        # rather than guessed; the warning below names the supported path.
        return None

    # A non-default data home refuses the SHARED write when the specs already
    # there belong to SOMEONE ELSE. The spec this write would produce pins THIS
    # instance's ``KIROCREW_HOME`` into every managed server entry
    # (``_managed_mcp_env``), and on the shared target that value is wrong for
    # every default-home instance under this ``$HOME``: their stubs then
    # resolve ``config_dir()`` to a home the real gateway never writes, and
    # every strict-identity tool fails closed with "signed pid mapping did not
    # verify". Durability is no defence — the reported writer was a durable
    # checkout, not a worktree or a temp clone, so the ephemerality arms below
    # never saw it.
    #
    # Ownership is read from the specs themselves (:func:`_existing_specs_are_mine`)
    # rather than inferred from mere existence, and that single invariant is
    # what keeps every population correct at once: a fresh relocated-home
    # install writes (no spec, no audience to poison — refusing would leave it
    # with no spec at all, the health check suppressed by this same guard, and
    # every turn dying at ``Mode 'kirocrew' not found``); an install that wrote
    # its own self-pinned specs keeps refreshing them (its own provenance
    # matches, so it cannot lock itself out); and specs pinned by a DIFFERENT
    # home — or pinned by nobody, the default-home writer's signature — refuse,
    # which is the reported poisoning shape. A concurrent FIRST boot of a
    # default- and an override-home gateway can still interleave (both read
    # "no spec"), but the wrong-owner state now CONVERGES instead of holding:
    # the default-home instance rewrites unconditionally on its next rebuild,
    # and this arm then reads that provenance and refuses from that point on.
    # This does not resurrect the offline-E2E regression recorded below: the
    # harness points ``KIRO_HOME`` at its own home, so its target is private
    # and exempted above, and an override that resolves to the default home
    # still writes (see :func:`shared_kiro_agents_writable`).
    if not shared_kiro_agents_writable() and _existing_specs_are_mine(target, own_home) is False:
        if audit:
            _warn_declined_home_once(
                "non-default-home",
                target,
                "Refusing to rewrite the shared agent home %s from a non-default "
                "data home (KIROCREW_HOME=%s%s): the specs would pin this "
                "instance's home into every managed MCP server entry and break "
                "strict session identity for the default-home gateway. "
                "This instance will use the existing specs instead. If no "
                "default-home install exists anymore (the data home was "
                "permanently relocated), the existing specs are stale leftovers: "
                "remove the kirocrew*.json files under %s and restart, and this "
                "instance will write its own.",
                target,
                own_home or "",
                " with KIROCREW_POD" if os.environ.get("KIROCREW_POD") else "",
                target,
            )
            sel().log_api_access(
                caller="system",
                operation="agent_home_write",
                outcome="denied",
                source="rebuild_agent_config",
                resources=str(target),
                error=(
                    f"non-default data home {own_home or 'pod'} refused write to "
                    f"shared agent home"
                ),
            )
        return kiro_agents_dir_path() / AGENT_FILENAME

    # Ephemerality must be POSITIVE evidence that this instance is throwaway.
    # "Has an isolated KIROCREW_HOME" is NOT that: a CI test gateway and a user
    # who permanently relocated their data home both look identical under that
    # rule, and neither should be stopped from writing its own specs: the offline
    # E2E gateway boots on a tmp data home and must still write its agent specs,
    # or it finds no agents. A pod needs no arm here: ``build_pod_env``
    # gives it its own ``KIRO_HOME``, so its target is its own dedicated directory
    # and the private-target exemption above already lets it through.
    #
    # A checkout under the system temp directory is the third positive signal:
    # like a linked worktree and a pod, its teardown is a matter of WHEN, not
    # whether — temp trees are reaped by the OS, by CI, and by the automation
    # that cloned them (a per-task scratch clone is created and deleted around a
    # single job). A spec stamped from one names a launcher venv, and possibly a
    # pinned data home, that stop existing when the tree goes. Both failure modes
    # are live: ENOENT-dead managed servers, and empty-credential
    # ``internal_auth_mismatch`` when the pinned home is recreated empty. This
    # is checked on the CHECKOUT location (``__file__``), not the data home, so
    # the offline E2E harness — which runs the REPO checkout on a temp data
    # home — is unaffected, exactly the breakage the note above warns about.
    #
    # An AppImage's runtime mount is carved back OUT of that arm. It sits under
    # the temp root (``/tmp/.mount_<name>XXXXXX``) and the mount itself is indeed
    # reaped on exit, but the temp signal's premise — a spec outliving the only
    # instance that would have written it — inverts here: a DURABLE install (the
    # ``.AppImage`` file on disk) stands behind the mount and re-runs this on
    # every start, and because the runtime picks a NEW random mount each launch,
    # rewriting the spec per start is the only way its managed servers ever
    # resolve. Declining would freeze the spec on a previous launch's mount path
    # and ENOENT every managed server — manufacturing on a shipped channel the
    # very symptom this guard exists to prevent — and on a fresh install would
    # leave no spec at all (``Mode 'kirocrew' not found``).
    # ``_in_ephemeral_tree`` is the same
    # AppImage-precise predicate the launcher installer uses; the temp-root rule
    # it declines is the one being narrowed here, not adopted.
    #
    # The temp arm also declines only when there is something to preserve. This
    # guard's entire remedy is "use the specs that already worked" — the log line
    # below says exactly that — and with no spec present there are none, so
    # declining does not protect a shared resource, it just leaves the install
    # dead (every turn fails with ``Mode 'kirocrew' not found``). The harm this
    # guard prevents is specifically an OVERWRITE of a working spec, which it
    # still refuses. A spec that exists but is
    # already stale stays stale, same as under the worktree arm — repairing it is
    # the durable install's job on its next start, and it rewrites unconditionally.
    # Deliberately scoped to this arm: the worktree and pod arms chose their
    # populations on their own grounds, so widening them is a separate decision,
    # not a side effect of this third signal.
    checkout = Path(__file__).resolve()
    temp_scratch = (
        _under_system_tmp(checkout)
        and not _in_ephemeral_tree(checkout)
        and (target / AGENT_FILENAME).exists()
    )
    ephemeral = _in_linked_git_worktree(checkout) or temp_scratch
    if not ephemeral:
        # A GRANT over the shared resource, so it is audited like the denial
        # below: every permission decision about the machine-wide agent home is
        # traceable from the audit log alone, matching how ``api_lessons_create``
        # records both its allow and deny branches. Only the shared-home decision
        # is recorded -- the two private-target returns above are not decisions
        # about a shared resource, so auditing them would add volume without
        # adding traceability.
        if audit:
            sel().log_api_access(
                caller="system",
                operation="agent_home_write",
                outcome="allowed",
                source="rebuild_agent_config",
                resources=str(target),
            )
        return None  # an ordinary install writing its own shared home

    if audit:
        _warn_declined_home_once(
            "ephemeral",
            target,
            "Refusing to rewrite the shared agent home %s from an ephemeral instance "
            "(checkout %s, data home %s): it would repoint the real install's MCP "
            "servers at this instance's venv and data home, and break them outright "
            "when it is torn down. This instance will use the existing specs instead. "
            "Deliberately no remedy is suggested here: redirecting the agent home via "
            "KIRO_HOME also relocates kiro-cli's session storage, which Kiro Crew still "
            "reads from the host path -- see kiro_home()'s scope caveat.",
            target,
            Path(__file__).resolve().parents[2],
            own_home or "default",
        )
        # This is a permission decision on a shared, security-relevant resource (the
        # specs carry every managed MCP server's command + env), so it belongs in the
        # audit trail and not only in the log: a silent refusal is indistinguishable
        # from a write that simply did not happen when reconstructing what an
        # ephemeral instance did to the host.
        sel().log_api_access(
            caller="system",
            operation="agent_home_write",
            outcome="denied",
            source="rebuild_agent_config",
            resources=str(target),
            error=(
                f"ephemeral instance (checkout {Path(__file__).resolve().parents[2]}, "
                f"data home {own_home or 'default'}) refused write to shared agent home"
            ),
        )
    return kiro_agents_dir_path() / AGENT_FILENAME
