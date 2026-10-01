"""Host-resource rows of ``kirocrew doctor``.

Memory-pressure preparedness, the sandbox's tmpfs roots, leftover kiro-cli
installers, and what the shared agents directory and the workspace root have
accumulated. Every scan is bounded and read-only: the doctor names what a sweep
would reclaim and deletes nothing.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from kiro_crew import cli_doctor
from kiro_crew.doctor_checks import render

# Alias count above which the skill-view census warns. The projection publishes
# one ``kirocrew-skill-view-*.json`` per distinct agent view into the shared
# agents directory -- spawns that derive the same view share one file -- and
# kiro-cli reads EVERY file there on startup, so the count is a startup cost for
# every session on the host. A healthy host carries roughly authored agents x
# workspaces; the measured trouble starts past a couple of thousand -- about 8s
# of prune walk per spawn at 2,360 files, and ``EMFILE: too many open files``
# from kiro-cli at 15k. A boot drain clears a backlog at gateway start and the
# per-spawn reclaim covers steady-state orphans, so a count above this is either
# a backlog this gateway has not drained yet or one it cannot drain (another
# data home's aliases, an unreadable lease record); the warning tells which.
_SKILL_VIEW_BACKLOG_WARN = 2000


# Where SwapTotal is read from. A module attribute (not inlined) so tests can
# point it at a fabricated meminfo file.
_PROC_MEMINFO = Path("/proc/meminfo")


def _swap_total_kib() -> int | None:
    """``SwapTotal`` from ``/proc/meminfo`` in KiB, ``None`` when unreadable.

    Read from procfs directly rather than shelling out to ``free``/``swapon``:
    the file is world-readable and parsing it cannot hang or prompt.
    """
    try:
        text = _PROC_MEMINFO.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        if line.startswith("SwapTotal:"):
            parts = line.split()
            try:
                return int(parts[1])
            except (IndexError, ValueError):
                return None
    return None


def _gateway_memory_lines() -> list[str]:
    """The ``session ceiling`` and ``gateway rss`` lines of the Memory Pressure section.

    The ceiling is ``session.watchdog_rss_max_mb`` from the loaded config (``0``
    = disabled, called out as such because an operator reading this section is
    usually asking "what stops a runaway session tree?"). The RSS is read from
    the live gateway's pid via the lock-holder oracle ``cli_perf`` already uses,
    so a stale recorded pid can never be reported as the gateway's memory; no
    live gateway prints "not running". Every failure degrades to a line saying
    so — this is advisory and must never abort doctor.
    """
    lines: list[str] = []
    try:
        ceiling = int(cli_doctor.KiroCrewConfig.load().session.watchdog_rss_max_mb)
    except Exception:
        lines.append("  session ceiling: ⚠️  could not read session.watchdog_rss_max_mb")
    else:
        if ceiling > 0:
            lines.append(
                f"  session ceiling: ✅ {ceiling} MiB per session process tree "
                "(session.watchdog_rss_max_mb; idle sessions above it are recycled)"
            )
        else:
            lines.append(
                "  session ceiling: ⏹ disabled (session.watchdog_rss_max_mb = 0) — "
                "nothing bounds a runaway session tree"
            )
    try:
        pid = cli_doctor._read_gateway_pid()
        if pid is None:
            lines.append("  gateway rss:     ⏹ not running")
        else:
            rss = cli_doctor._gateway_rss_bytes(pid)
            if rss is None:
                lines.append(f"  gateway rss:     ⚠️  pid {pid} alive but RSS unreadable")
            else:
                lines.append(f"  gateway rss:     {rss // (1024 * 1024)} MiB (pid {pid})")
    except Exception:
        lines.append("  gateway rss:     ⚠️  could not determine (probe failed)")
    return lines


def _doctor_memory_pressure(issues: list[str]) -> None:
    """Report whether the host can degrade gracefully under memory pressure.

    A Linux host with zero swap and no userspace OOM killer has no pressure
    release valve: sustained memory pressure evicts file-backed pages (running
    code included) faster than they re-fault in, and the host livelocks —
    unresponsive for minutes, sometimes until a power cycle — before the kernel
    OOM killer's conservative heuristics fire. Either protection alone (swap to
    absorb the spike, or earlyoom/systemd-oomd to kill a hog early) prevents
    the freeze, so this warns only when BOTH are absent. When detection is
    inconclusive it reports "unknown" instead of warning.

    Advisory only (never appended to ``issues``): swap sizing and OOM-killer
    policy are host configuration the user owns — doctor reports the exposure,
    it does not fail the install over it. Linux-only: the freeze mode and both
    detection sources are Linux-specific.
    """
    del issues  # advisory-only diagnostic; keeps the call-site signature uniform
    print("\nMemory Pressure")
    # What is bounding memory right now, on every platform: the gateway's own
    # resident set and the per-session tree ceiling the cleanup watchdog
    # recycles at. Printed before the Linux-only freeze check so a Windows or
    # macOS operator still sees the numbers that matter for a runaway tree.
    for line in _gateway_memory_lines():
        print(line)
    if not sys.platform.startswith("linux"):
        print(
            f"  freeze risk: ⏹ not applicable ({sys.platform} — the swap/OOM-killer "
            "check reads Linux procfs)"
        )
        return

    swap_kib = _swap_total_kib()
    if swap_kib is None:
        print("  swap:        ⚠️  could not read SwapTotal from /proc/meminfo — check skipped")
        return
    if swap_kib > 0:
        print(f"  swap:        ✅ {swap_kib / 1048576:.1f} GiB configured")
    else:
        print("  swap:        ⏹ none (SwapTotal = 0)")

    killer = cli_doctor._detect_userspace_oom_killer()
    if isinstance(killer, str):
        print(f"  oom killer:  ✅ {killer} active")
    elif killer is False:
        print(
            "  oom killer:  ⏹ none active (checked: "
            + ", ".join(cli_doctor._OOM_KILLER_UNITS)
            + ")"
        )
    else:
        print("  oom killer:  ⏹ could not determine (no systemctl, or the probe failed)")

    if swap_kib > 0 or isinstance(killer, str):
        return
    if killer is None:
        # Uncertain detection must not warn — a container or non-systemd host
        # may run a killer doctor cannot see.
        print("  freeze risk: ⏹ unknown — no swap, and OOM-killer detection was inconclusive")
        return
    print("  freeze risk: ⚠️  host can freeze under sustained memory pressure")
    print("               With no swap and no userspace OOM killer, memory pressure")
    print("               thrashes file-backed pages and the host can livelock before")
    print("               the kernel OOM killer intervenes.")
    print("               Fix: add swap, enable systemd-oomd, or install earlyoom.")


# ── Runtime tmpfs headroom (sandbox mount-source roots) ──────────────────────
# Warn thresholds for the tmpfs roots the sandbox launcher stages bind-mount
# sources on. Leaked ``tmp*`` mount dirs once filled ``/run/user/$UID`` until its
# inodes ran out, at which point every tool spawn failed with a bare ``rc=1``.
# Inode exhaustion is the more likely face on a tmpfs (each leaked dir is tiny
# but costs an inode), so both free-space and free-inode fractions are checked,
# plus an absolute inode floor: a small tmpfs at 11% free inodes can still be a
# few hundred dirs from failure.
_TMPFS_FREE_PCT_WARN = 10.0
_TMPFS_FREE_INODES_FLOOR = 1000


def _runtime_tmpfs_roots() -> list[str]:
    """The roots the sandbox would stage mount sources on, in launcher order.

    Reuses the sandbox's own chooser rather than hardcoding ``/run/user`` so a
    change to the launcher's fallback chain moves this check with it.
    """
    return cli_doctor.sandbox._mount_source_candidate_roots()


def _tmpfs_usage(root: str) -> tuple[float, float, int, int] | None:
    """``(free_space_pct, free_inode_pct, free_inodes, tmp_entries)`` for *root*.

    ``None`` when the root does not exist or cannot be measured, or when the
    platform has no ``os.statvfs`` (Windows; the doctor section that calls this
    is Linux-only, so this is belt-and-braces for direct callers). ``tmp_entries``
    counts the names carrying the sandbox launcher's mount-source prefix
    (``kirocrew_sb_<pid>_``), so a warning can say how much of the pressure is
    Kiro Crew's own; every other temporary entry belongs to somebody else and
    is deliberately not counted, so the cleanup advice never points at it. A
    filesystem that reports no inode accounting (``f_files == 0``) reads as
    100% free inodes rather than as exhausted.
    """
    statvfs = getattr(os, "statvfs", None)
    if statvfs is None:
        return None
    try:
        st = statvfs(root)
    except OSError:
        return None
    free_space_pct = 100.0 * st.f_bavail / st.f_blocks if st.f_blocks else 100.0
    if st.f_files:
        free_inode_pct = 100.0 * st.f_favail / st.f_files
        free_inodes = int(st.f_favail)
    else:
        # No inode accounting (btrfs, some FUSE mounts): both readings say
        # "not a constraint" so neither the percentage nor the absolute floor
        # below can fire on a filesystem that cannot run out of inodes.
        free_inode_pct = 100.0
        free_inodes = _TMPFS_FREE_INODES_FLOOR
    try:
        with os.scandir(root) as it:
            tmp_entries = sum(1 for e in it if e.name.startswith(cli_doctor._MOUNT_SOURCE_PREFIX))
    except OSError:
        tmp_entries = 0
    return free_space_pct, free_inode_pct, free_inodes, tmp_entries


def _doctor_runtime_tmpfs(issues: list[str]) -> None:
    """Warn when a sandbox tmp root is close to running out of space or inodes.

    The failure this pre-empts is silent until total: leaked mount-source dirs
    accumulate in the runtime tmpfs, and once its inodes are gone every sandboxed
    tool spawn fails with nothing more than ``rc=1``. Reclaim runs on the
    gateway, but an operator looking at a wall of ``rc=1`` needs somewhere that
    names the disk. Appended to *issues*: a full tmp root breaks every tool, so
    it is a fault, not host trivia. Linux only -- the launcher is.
    """
    if not sys.platform.startswith("linux"):
        return
    print("\nRuntime tmpfs")
    for root in _runtime_tmpfs_roots():
        usage = _tmpfs_usage(root)
        if usage is None:
            print(f"  {root}: ⏭  not present or unreadable")
            continue
        free_space_pct, free_inode_pct, free_inodes, tmp_entries = usage
        detail = (
            f"{free_space_pct:.0f}% space free, {free_inode_pct:.0f}% inodes free "
            f"({free_inodes} inodes), {tmp_entries} {cli_doctor._MOUNT_SOURCE_PREFIX}* entries"
        )
        low_space = free_space_pct < _TMPFS_FREE_PCT_WARN
        low_inodes = free_inode_pct < _TMPFS_FREE_PCT_WARN or free_inodes < _TMPFS_FREE_INODES_FLOOR
        if low_space or low_inodes:
            what = "inodes" if low_inodes and not low_space else "space"
            if low_space and low_inodes:
                what = "space and inodes"
            print(f"  {root}: ⚠️  low on {what} — {detail}")
            print(
                "               Sandboxed tool spawns fail with rc=1 once this fills. "
                f"Kiro Crew's own leaked mount dirs are the {cli_doctor._MOUNT_SOURCE_PREFIX}* "
                "entries; a gateway restart reclaims them. Other entries there "
                "belong to other applications: leave them alone."
            )
            issues.append(f"runtime tmpfs {root} low on {what} ({detail})")
        else:
            print(f"  {root}: ✅ {detail}")


# ── kiro-cli installer residue ────────────────────────────────────────────────
# kiro-cli runs its auto-update check on STARTUP — the ``app.disableAutoupdates``
# setting is documented as "Disable automatic updates on startup" — and Crew
# spawns a FRESH kiro-cli per session (``AcpRuntime`` is constructed per session
# in ``providers/acp.py`` and ``session.py``, and again per Code Review Sage
# worker). So that check runs once per process START, not once per host per
# release.
#
# On Windows the running executable cannot be replaced, so the downloaded
# installer can never be applied while a Crew ACP child holds the binary — and
# the "update pending" state is not cleared after an upgrade either. Nothing in
# that loop is self-limiting: one installer is left behind per process start, and
# the residue reaches tens of gigabytes.
#
# Crew cannot fix the updater, and must NOT disable updates on the user's behalf:
# ``app.disableAutoupdates`` is a per-user setting shared with their own
# interactive CLI, so setting it silently would suppress their security updates.
# What Crew can do is stop the residue being invisible, since it is Crew's
# per-session spawning that turns a stale flag into tens of gigabytes.
_CLI_INSTALLER_GLOB = "kiro-installer*"

# One file can be a download still in flight; two or more is residue, because a
# failed apply leaves the file behind and the next process start fetches another.
_CLI_INSTALLER_RESIDUE_MIN = 2

# The temp dir is shared with every other process on the host and can hold a very
# large number of entries, so a diagnostic must not walk it unbounded.
# Non-recursive by design: the installer lands at the top level.
_CLI_INSTALLER_SCAN_CAP = 512


def _scan_cli_installer_residue(temp_dir: Path) -> tuple[int, int]:
    """Return ``(count, total_bytes)`` for leftover kiro-cli installers in *temp_dir*.

    Bounded and non-raising: the scan stops at :data:`_CLI_INSTALLER_SCAN_CAP`
    matches, and an entry that vanishes mid-scan — another process cleaning up,
    or the updater itself — is skipped rather than aborting the whole doctor run.
    An unreadable temp dir reports "nothing found" for the same reason.
    """
    count = 0
    total = 0
    try:
        for entry in temp_dir.glob(_CLI_INSTALLER_GLOB):
            try:
                if not entry.is_file():
                    continue
                total += entry.stat().st_size
            except OSError:
                # Raced with a delete, or unreadable: one bad entry must not
                # abort a diagnostic.
                continue
            count += 1
            if count >= _CLI_INSTALLER_SCAN_CAP:
                break
    except OSError:
        return (0, 0)
    return (count, total)


def _doctor_cli_installer_residue(issues: list[str]) -> None:
    """Report leftover kiro-cli auto-update installers piling up in the temp dir.

    Silent on a healthy host — the common case, and every case on a platform that
    can replace a running binary — so a normal doctor run gains no noise. This
    speaks only when residue is actually present, which is why it is not gated on
    ``platform.system() == "Windows"``: the gate is the evidence on disk, so the
    check still fires if this failure mode ever appears on another platform.
    """
    # gettempdir() itself probes candidate directories and raises when none is
    # usable, so it must be inside the guard too: a host with a full or
    # unwritable temp volume is exactly the host most in need of the rest of the
    # doctor run, and must not get a traceback instead of it.
    try:
        temp_dir = Path(tempfile.gettempdir())
    except OSError:
        return
    count, total = _scan_cli_installer_residue(temp_dir)
    if count < _CLI_INSTALLER_RESIDUE_MIN:
        return

    # Capped scans undercount, so say so rather than printing a precise-looking
    # number that is actually a floor. This applies to the SIZE as well: the scan
    # stopped summing at the cap, so the total is a floor exactly as the count is,
    # and rendering it as exact next to a "512+" count would contradict itself.
    capped = count >= _CLI_INSTALLER_SCAN_CAP
    count_label = f"{count}+" if capped else str(count)
    if total >= 1073741824:
        size_label = f"{total / 1073741824:.2f} GiB"
    else:
        size_label = f"{total / 1048576:.1f} MiB"
    if capped:
        size_label = f"≥ {size_label}"

    print("\nkiro-cli installer residue")
    print(f"  files:       ⚠️  {count_label} in {temp_dir}")
    print(f"  reclaimable: {size_label}")
    print("               Auto-update downloads that could not be applied while")
    print("               kiro-cli was running, and are not cleaned up. Crew starts")
    print("               a kiro-cli per session, so one accumulates per start.")
    print(f"               Fix: delete {_CLI_INSTALLER_GLOB} from {temp_dir}, then stop")
    print("               the gateway and run `kiro-cli update` deliberately.")
    print("               To stop the downloads: `kiro-cli settings")
    print("               app.disableAutoupdates true` — note this is per-user, so it")
    print("               also pauses updates for your own interactive kiro-cli.")
    issues.append("kiro-cli installer residue in temp")


def _doctor_agents_janitor(issues: list[str], sweep_backups: bool) -> None:
    """Report aged orphaned atomic-write temps and stale backups in the agents dir.

    The shared kiro agents directory accumulates ``<base>.json.<digits>.tmp``
    orphans and ``*.bak-<digits>`` / ``*.json.bak.<digits>`` backups from the
    several independent writers that install agents there; nothing else removes
    them. ``kirocrew doctor`` REPORTS what a sweep would reclaim but never
    deletes anything itself (``dry_run=True``) — a diagnostic you run *because
    something broke* must not silently unlink files, including recovery backups,
    in the same invocation. Actual deletion is left to the fire-and-forget boot
    sweep, and the report mirrors that sweep's scope: backups are only counted
    when ``agent.sweep_agents_backups`` is enabled (*sweep_backups*), since Kiro
    Crew authors none of them and the boot sweep leaves foreign backups alone by
    default. Advisory only (never appended to ``issues``): reclaimable junk is
    housekeeping, not a setup fault, and the scan is fail-open so it can never
    abort the run.
    """
    del issues  # advisory-only diagnostic; keeps the call-site signature uniform
    print("\nAgents Directory")
    agents_dir = cli_doctor._agents_dir()
    result = cli_doctor.sweep_agents_dir(agents_dir, dry_run=True, sweep_backups=sweep_backups)
    if result.removed:
        mib = result.freed_bytes / 1048576
        print(
            f"  janitor:     🧹 {result.removed} stale temp/backup file(s) "
            f"reclaimable ({mib:.1f} MiB) — the gateway sweeps these on boot"
        )
        for name in result.removed_names:
            # ``!r`` on the name: this directory is shared with foreign writers,
            # so a crafted filename could otherwise smuggle a terminal-control
            # (ANSI/OSC) escape sequence straight to the operator's terminal.
            print(f"{render._INDENT}- {name!r}")
    else:
        print("  janitor:     ✅ no stale temp/backup files to reclaim")
    _doctor_skill_view_census(agents_dir)
    _doctor_skill_view_residue(agents_dir)
    _doctor_run_dirs()


# Orphaned sidecars or leftover ``<alias>.lock`` files above which the doctor
# warns. The gateway sweeps both in bounded batches, so steady state is near
# zero; a count this high is a backlog it has not drained yet.
_SKILL_VIEW_RESIDUE_WARN = 500


def _doctor_skill_view_residue(agents_dir: Path) -> None:
    """Report, in one line, the skill-view residue and an external rewriter.

    Advisory and read-only. Two signals the alias census cannot give: files the
    projection left around aliases that are gone (ownership sidecars, and the
    empty ``<alias>.lock`` files a spec-rewriting launcher leaves), and aliases
    whose bytes differ from what the projection recorded -- another program is
    rewriting the agents directory, the precondition of alias growth and of the
    "not installed" failure. A
    rewriter is not a fault by itself; the line says so and names the rollback
    switch in case sessions are failing.
    """
    from kiro_crew.agent_sdk.drivers import acp as acp_driver

    counts = acp_driver.skill_view_residue_census(agents_dir)
    orphans = counts.get("orphan_sidecars", 0)
    locks = counts.get("alias_locks", 0)
    rewritten = counts.get("rewritten", 0)
    floor = "+" if counts.get("truncated", 0) else ""
    metadata_dir, _lease_dir = acp_driver.skill_view_sidecar_dirs()
    warn = orphans > _SKILL_VIEW_RESIDUE_WARN or locks > _SKILL_VIEW_RESIDUE_WARN or rewritten
    print(
        f"  skill-view residue: {'⚠️ ' if warn else '✅'} {orphans}{floor} ownership sidecar(s) in"
        f" {metadata_dir}/ with no alias, {locks}{floor} leftover alias .lock file(s),"
        f" {rewritten}{floor} alias(es) rewritten by another program"
    )
    if orphans > _SKILL_VIEW_RESIDUE_WARN or locks > _SKILL_VIEW_RESIDUE_WARN:
        print(
            f"{render._INDENT}The gateway removes the ones it can prove are its own residue"
            f" (this home's sidecars, unheld empty locks) in bounded batches at boot and on"
            f" every spawn; restart it once to drain the backlog."
        )
    churning = acp_driver.skill_view_churning_env_keys(agents_dir)
    if churning:
        print(
            f"{render._INDENT}⚠️ env value(s) differing across one agent's skill views:"
            f" {', '.join(render._safe_display(label) for label in churning)}. If a launcher"
            f" re-stamps one on every launch, add its"
            f" key to KIROCREW_SKILL_VIEW_VOLATILE_ENV so it stops naming a new view per launch."
        )
    if rewritten:
        print(
            f"{render._INDENT}Another program rewrites the specs in {agents_dir} (a sandbox or"
            f" credential launcher does this on every launch). Kiro Crew tolerates it; if"
            f" sessions still fail with 'Agent spec ... is not installed', set"
            f" KIROCREW_NATIVE_SKILL_PROJECTION=0 for the gateway and report it."
        )


# Unmarked run directories above which the doctor warns. Each is one directory
# holding one small file; the count matters as a listing cost on the workspace
# root, which every derived-cwd spawn's ``mkdir`` re-enumerates.
_RUN_DIR_BACKLOG_WARN = 1000


def _doctor_run_dirs() -> None:
    """Report, in one line, the run directories the gateway's sweep cannot reclaim.

    Advisory and read-only. A subagent or stateless cron run gets a directory
    under the workspace root that the provider marks at first start and reclaims
    at shutdown; the gateway sweeps what a dead predecessor of its own data home
    left. Two figures from one bounded walk, judged by the sweep's own rule:
    directories from builds that wrote no marker (a name is not provenance, so
    the sweep deletes nothing it cannot prove Crew made), and marked directories
    this data home cannot act on -- another data home's, an unreadable marker,
    or a gateway the pid ledger still retains entries for. Named, never done:
    the doctor deletes nothing.
    """
    from kiro_crew.config.loader import workspace_root
    from kiro_crew.session_pid import retained_gateway_pids
    from kiro_crew.session_work_dir import DERIVED_NAME_RE, RUN_DIR_MARKER, count_run_dirs

    try:
        # Resolve only: the default resolver creates the tree, and a read-only
        # report must not leave a workspace behind where no gateway ever ran.
        root = workspace_root(create=False)
    except OSError:
        return
    if not root.is_dir():
        print("  run dirs:    ✅ no workspace root yet, so no run directories")
        return
    try:
        retained = retained_gateway_pids()
    except OSError:
        print("  run dirs:    ⚠️  the session pid ledger cannot be read; census skipped")
        return
    census = count_run_dirs(root, retained_gateway_pids=retained)
    if not census.unmarked and not census.refused:
        print("  run dirs:    ✅ no run directories left behind that the sweep cannot reclaim")
        return
    suffix = "+" if census.floor else ""
    warn = census.unmarked > _RUN_DIR_BACKLOG_WARN or census.refused > 0
    print(
        f"  run dirs:    {'⚠️ ' if warn else '✅'} under {root}: {census.unmarked}{suffix} run"
        f" director(ies) carry no {RUN_DIR_MARKER} marker (left by a build that wrote none);"
        f" {census.refused}{suffix} marked director(ies) this data home cannot reclaim (another"
        f" data home's, an unreadable marker, or a gateway the pid ledger still retains)"
    )
    if census.unmarked > _RUN_DIR_BACKLOG_WARN:
        print(
            f"{render._INDENT}The gateway reclaims only marked run directories. With the gateway"
            f" stopped, move directories matching {DERIVED_NAME_RE.pattern} that hold only"
            f" .kiro/settings/cli.json out of {root}; a live run recreates its own."
        )


def _doctor_skill_view_census(agents_dir: Path) -> None:
    """Report how many projected skill-view aliases the agents directory holds.

    Advisory and read-only, like the janitor line above it. The count matters
    because kiro-cli enumerates every file in this directory on every startup
    and the projection writes one alias per distinct agent view, shared by
    every spawn of that agent: a backlog from a build that predates the
    lease-based reclaim reached 28k files on one host and made every session
    start crawl. The gateway drains its own home's unreferenced aliases -- the
    whole backlog at boot in lock-bounded batches, a bounded number per spawn
    after that; the report says
    exactly which share that covers -- not aliases another data home owns, not
    lease-named ones while their lease is held -- and refuses to promise any
    drain while a lease record is unreadable, since the reclaim then keeps
    everything. Once the census hit a retention bound its counts are floors and
    the derived ones are not printed at all. The manual fallback is named,
    never performed, and is a move rather than a delete: the doctor cannot
    prove who authored a file that merely carries the prefix, and a move is
    undoable.
    """
    from kiro_crew.agent_sdk.drivers import acp as acp_driver

    counts = acp_driver.skill_view_alias_census(agents_dir)
    total = counts.get("total", 0)
    leased = counts.get("leased", 0)
    foreign_unreferenced = counts.get("foreign_home", 0)
    foreign_leased = counts.get("foreign_leased", 0)
    foreign = foreign_unreferenced + foreign_leased
    unreadable = counts.get("unreadable_leases", 0)
    truncated = bool(counts.get("truncated", 0))
    metadata_dir, lease_dir = acp_driver.skill_view_sidecar_dirs()
    alias_glob = f"{cli_doctor.NATIVE_SKILL_ALIAS_PREFIX}*.json"
    # Two Kiro Crew data homes share this directory whenever they share
    # ``~/.kiro``; the remedy must then stop every gateway that uses it, not
    # only the one this doctor speaks for.
    stopped = (
        "with every gateway that uses this agents directory stopped"
        if foreign
        else "with the gateway stopped"
    )

    floor = "+" if truncated else ""
    detail = f"{total}{floor} {alias_glob} alias(es)"
    if total:
        detail += f" ({leased}{floor} named by a lease record"
        # Once a bound was hit "not named" is total minus a floor, which is
        # neither a floor nor a ceiling, so only the measured counts are shown.
        if not truncated:
            detail += f", {total - leased} not"
        if foreign:
            detail += f", {foreign}{floor} owned by another Kiro Crew home"
        detail += ")"
    warn = bool(unreadable) or total > _SKILL_VIEW_BACKLOG_WARN
    print(f"  skill views: {'⚠️ ' if warn else '✅'} {detail}")
    if truncated:
        print(f"{render._INDENT}(floors: the census stopped at its retention bound)")
    if unreadable:
        print(
            f"{render._INDENT}{unreadable} lease record(s) in {lease_dir}/ cannot be read, and"
            f" the reclaim keeps every alias while one exists. {stopped[0].upper()}"
            f"{stopped[1:]}, move that directory out of the agents directory; every"
            f" live projection republishes its own lease."
        )
    if total <= _SKILL_VIEW_BACKLOG_WARN:
        return
    if unreadable:
        drain = " Nothing is reclaimed until the unreadable lease record(s) above are gone."
    elif truncated:
        # Past the lease bound the census did not read every record, and one
        # unreadable record it did not reach would stop the reclaim entirely.
        drain = (
            " Unscanned lease records leave reclaimability unknown: the gateway"
            " reclaims this home's unreferenced aliases a bounded number per spawn"
            " only while every lease record is readable."
        )
    else:
        drain = (
            f" On every spawn the gateway reclaims a bounded number of the"
            f" {total - leased - foreign_unreferenced} this home owns and no lease names."
        )
    if leased - foreign_leased > 0:
        drain += (
            " This home's lease-named aliases are kept while their lease is held; a"
            " crash-stale lease is reclaimed on the next spawn."
        )
    if foreign:
        drain += (
            f" The {foreign}{floor} another Kiro Crew home owns never drain here;"
            f" only that home's gateway reclaims them."
        )
    print(
        f"{render._INDENT}kiro-cli reads every file here on startup, so this many slows every"
        f" session start.{drain}"
    )
    print(
        f"{render._INDENT}To clear it at once: {stopped}, move the {alias_glob} files and the"
        f" {metadata_dir}/ directory out of the agents directory (a move is undoable;"
        f" the doctor never deletes). Every spawn republishes the aliases it needs;"
        f" authored agents keep their own names and are not touched."
    )
