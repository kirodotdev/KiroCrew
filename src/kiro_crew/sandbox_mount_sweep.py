"""Reclaim the bind-mount sources the Linux namespace launcher leaves on tmpfs.

The launcher stages an empty directory or file for every path it masks, named
``kirocrew_sb_<pid>_...`` on a tmpfs root, and binds it over the target. The kernel
pins a source for its mount's lifetime, so the launcher cannot remove it and it
orphans when the sandboxed process exits; left alone the pile exhausts the session
runtime tmpfs. This module decides which of those entries are provably ours and no
longer bound by any live mount namespace, and removes only those:

- :func:`_mount_pinned_source_names` reads every readable task's mountinfo to find
  the sources a live namespace still binds, and reports through
  :class:`_PinScanCoverage` whether every task that could hold one was read;
- :func:`_cleanup_stale_sandbox_mount_sources` reclaims pid-keyed entries whose pid
  is dead or past the age backstop, by kind;
- :func:`_cleanup_legacy_mount_source_residue` reclaims the older pid-less residue;
- :func:`_cleanup_retired_acp_snapshot_dir` removes a retired snapshot directory.

``kiro_crew.sandbox.cleanup_stale_sandbox_profiles`` runs them after its own sweep of
launcher scripts and profiles. ``kiro_crew.sandbox`` re-exports every name here, and a
patch through it lands here, which is how the test floor keeps the sweep off the
host's real ``/run/user``, ``/dev/shm`` and ``/proc``.
"""

from __future__ import annotations

import errno
import functools
import logging
import os
import re
import shutil
import stat
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew import platform_compat

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

# Logged under the sandbox's name: the sweep is part of the sandbox's cleanup pass,
# and operator log filters and level settings key on that name.
logger = logging.getLogger("kiro_crew.sandbox")


# Bind-mount SOURCES staged by the namespace launcher (empty dirs/files bound
# over credential paths, plus the SSH shadow dir). The kernel pins a source for
# the mount's lifetime, so the launcher cannot unlink them and they orphan when
# the sandboxed process exits. The launcher names them with this prefix plus
# its own pid ("kirocrew_sb_<pid>_..."); the pid is the liveness key the
# janitor probes. The age threshold backstops recycled pids for the removals
# that stay safe against a live mount (plain files and empty dirs).
_MOUNT_SOURCE_PREFIX = "kirocrew_sb_"
_MOUNT_SOURCE_MAX_AGE_SECONDS = 24 * 3600

# Pin-scan stabilization budget: a mount-namespace holder can fork a successor
# and exit between the /proc listing and its own mountinfo read, so a pass
# that observed a vanish rescans newly appeared pids; past this many passes
# coverage is reported as unproven instead of looping.
_PIN_SCAN_MAX_PASSES = 3
_MOUNT_TABLE_CACHE_MAX_ENTRIES = 256
_MOUNT_TABLE_CACHE_MAX_BYTES = 64 * 1024 * 1024


class _PinScanCoverage:
    """Whether the pin scan read every task that could hold a sandbox source.

    Filled by :func:`_mount_pinned_source_names` beside its host-wide
    ``complete`` flag. That flag drops for reasons that cannot involve a
    sandbox source -- another user's unreadable task, or one departing on the
    final pass -- and on a busy host it can stay down indefinitely, which is
    how the directory class came to accumulate without bound. ``covered`` asks
    the narrower question the gate needs: was every task that could hold a
    source this uid staged read, and did none depart between the final
    listing and its read? Those are this uid's tasks (the sandboxed child
    keeps this uid with NO_NEW_PRIVS), the overflow uid's (how a nested user
    namespace stats) and root's (root can ``nsenter`` any namespace) -- so a
    ``hidepid`` procfs, which hides root's tasks, clears it too. A task that
    departs on the FINAL pass may have handed its namespace to a child forked
    after that listing, which only a re-listing could have seen and none
    followed, so such a departure clears ``covered``; one on an earlier pass
    was followed by a re-listing and is accounted for. When ``covered`` holds
    the pinned set is authoritative for every possible holder.
    """

    __slots__ = ("covered",)

    def __init__(self) -> None:
        self.covered = True


def _task_uid(proc_root: str, name: str) -> int | None:
    """The uid ``/proc/<name>`` stats as, or None once it is gone."""
    try:
        return os.stat(os.path.join(proc_root, name)).st_uid
    except OSError:
        return None


# The overflow uid: what /proc/<pid> stats to for a process whose uid has no
# mapping in the reader's user namespace. Such a holder CAN be binding our
# sources, so it is a coverage gap, not a foreign user. The value is a
# writable sysctl, so it is read from the host; an unreadable sysctl answers
# None, and the scan then treats EVERY unreadable non-own uid as a gap.
_OVERFLOW_UID_SYSCTL = "/proc/sys/kernel/overflowuid"


@functools.lru_cache(maxsize=1)
def _overflow_uid() -> int | None:
    try:
        return int(Path(_OVERFLOW_UID_SYSCTL).read_text().strip())
    except (OSError, ValueError):
        return None


def _parse_pid_segment(pid_str: str) -> int | None:
    """Parse a pid segment from a sweep-owned filename, or None to skip.

    Both sweeps (launcher scripts and mount sources) only ever WRITE an ASCII
    positive decimal pid, so anything else is a foreign or planted name and
    must fail toward "skip": non-ASCII decimals (``int()`` would accept
    them), pid ``0`` (``os.kill(0, 0)`` probes the caller's own process group
    and always reads alive), zero-padded segments, and — belt-and-braces,
    NAME_MAX keeps real names far shorter than the int/str conversion limit —
    a ``ValueError`` from ``int()`` itself.
    """
    if not pid_str.isascii() or not pid_str.isdecimal() or pid_str.startswith("0"):
        return None
    try:
        return int(pid_str)
    except ValueError:
        return None


def _mount_source_candidate_roots() -> list[str]:
    """The tmpfs roots the namespace launcher stages bind-mount sources on.

    Mirrors the launcher's own ``_tmpfs_src`` candidate chain —
    ``/run/user/$UID``, ``/dev/shm``, then the system default tempdir (the
    ``_tmpfs_src=None`` fallback). The tempdir entry is the gateway's own
    ``tempfile.gettempdir()``, which matches the launcher's fallback only while
    both resolve the same ``TMPDIR``; a launcher spawned with a divergent,
    persistent ``TMPDIR`` stages its fallback entries somewhere this sweep
    never visits, and nothing else reclaims them — a real limitation, reached
    only when NO tmpfs candidate exists at all. ``os.getuid`` is
    POSIX-only; the launcher itself is Linux-only, so a platform without it
    simply has no per-user runtime root to sweep.
    """
    roots: list[str] = []
    getuid = getattr(os, "getuid", None)
    if getuid is not None:
        roots.append(f"/run/user/{getuid()}")
    roots.append("/dev/shm")
    roots.append(tempfile.gettempdir())
    return roots


def _mount_pinned_source_names(
    proc_root: str = "/proc",
    *,
    matcher: Callable[[str], bool] | None = None,
    coverage: _PinScanCoverage | None = None,
) -> tuple[set[str], bool]:
    """Entry names of sandbox mount sources referenced by a live mount, plus
    whether the scan positively covered every namespace a PROCESS could be
    binding one from.

    A bind mount records its source as the ``root`` field (field 4) of a
    ``/proc/<pid>/mountinfo`` line, and the staged sources are direct children
    of a tmpfs root, so the basename of that field is the staged entry's name
    (mkdtemp names carry no spaces, so mountinfo's octal escaping never
    applies to them). Only prefix-shaped basenames are collected — a foreign
    mount whose root merely resembles a path cannot pin anything. Keying is by
    basename, so two identically-named entries on different roots would share
    a pin; mkdtemp's random suffix makes that vanishingly rare, and the error
    lands on the retention side.

    The launcher's mounts live in the sandboxed child's PRIVATE namespace —
    invisible in the gateway's own mountinfo — and that namespace can outlive
    the launcher pid through any surviving descendant (a build daemon, for
    example), so every readable pid's mountinfo is consulted. Coverage is
    what makes the answer usable, and it is established positively:

    - The listing must show pid 1. ``hidepid``/``subset=pid`` procfs hides
      other users' processes entirely — including a root holder that entered
      a sandbox namespace — and pid 1 always exists, so its absence proves
      the listing is filtered and the scan reports incomplete. It still reads
      every pid the filtered listing DOES show (this uid's own processes are
      never hidden from it, and every sandbox descendant keeps this uid), so
      the pins a visible holder contributes reach the caller regardless: the
      directory gate honours ``pinned`` before any other evidence.
    - A holder can fork a successor and exit between the pid listing and its
      own mountinfo read (the read then raises FileNotFoundError). The
      successor was forked BEFORE the exit, so it is visible to the very next
      listing: the scan re-lists after any pass that observed a vanish, and
      finishes on a pass that saw none. A pid appearing WITHOUT a vanish
      needs no rescan — it is either in a namespace whose surviving holders
      this scan already read, or in a brand-new namespace, which can only
      bind brand-new (fresh, live-pid) entries that are never reclaim
      candidates in the same sweep. A scan still churning after
      ``_PIN_SCAN_MAX_PASSES`` cannot prove coverage and reports incomplete.
      (A successor recycled onto an already-seen pid NUMBER inside this
      window escapes the re-listing; that needs the full pid space to wrap
      within the scan's milliseconds, and the residual error is
      retention-side only on the next sweep.)
    - A read failure is forgiven in two cases and counts as a coverage gap
      otherwise. ``EINVAL`` says that TASK has no ``nsproxy``, which is not yet
      a statement about the NAMESPACE: a thread-group leader can exit through
      ``pthread_exit`` while sibling threads keep running, and threads share the
      nsproxy, so a live namespace can sit behind a zombie leader while
      ``proc_root`` — which lists only leaders — shows nothing else to scan. So
      the group is consulted through ``/proc/<pid>/task/<tid>/mountinfo``, with
      exactly two outcomes per sibling and no third: one that READS contributes
      pins and nothing else, and one that does not read, for ANY reason,
      makes coverage unprovable. Departed siblings are counted rather than
      excused, because one exiting between the listing and its read may have
      spawned a successor THREAD first, and a thread is invisible to the outer
      re-listing (which enumerates thread-group leaders only), so no re-listing
      can recover it. Tasks in one group can also hold DIFFERENT mount
      namespaces, since a thread may ``unshare(CLONE_NEWNS)``, so a readable
      sibling never speaks for an unreadable one. When every sibling reads, the
      leader's own departure still makes this a vanish, because a departing task
      may have handed its namespace to a PROCESS forked after this pass's
      listing and only a re-listing can see that. A LIVE leader that could be a
      holder (this uid's, the overflow uid's, root's) has its siblings read the
      same way, since a thread can ``unshare(CLONE_FS)`` + ``setns`` into a
      namespace its leader is not in; a sibling that departed mid-read has its
      whole group re-read on the next pass, one that would not read for any
      other reason makes coverage unprovable. Any OTHER errno on the leader
      is forgiven only when the pid provably belongs to a DIFFERENT real user
      (its ``/proc/<pid>`` stats to a uid that is not ours, not root, and not
      the host's overflow uid) — such a process cannot be binding a source this
      uid staged, because the sandboxed child keeps this uid and NO_NEW_PRIVS.
      Root can enter any namespace, and a holder in a foreign user namespace
      stats as the overflow uid, so those count as coverage gaps.

    A namespace held only by an nsfs fd or a bind-mounted ``ns/mnt`` — zero
    member processes — has no mountinfo to scan and is out of scope; the
    launcher never creates one. ``complete=False`` means absence-of-pin was
    NOT established host-wide. When the caller passes ``coverage``, the scan
    also answers the narrower question the directory gate needs
    (:class:`_PinScanCoverage`): whether every task that could be a launcher
    descendant — this uid's and the overflow uid's — was read, with none
    departing between the final listing and its read. Root's tasks count as
    possible holders too (root can ``nsenter`` any namespace), so a hidden or
    unreadable root task — a filtered procfs included — lowers coverage along
    with ``complete``; another user's unreadable or departing task lowers only
    ``complete``.
    """
    pinned: set[str] = set()
    complete = True
    seen: set[str] = set()
    getuid = getattr(os, "getuid", None)
    own_uid = getuid() if getuid is not None else None
    overflow_uid = _overflow_uid()
    # Default: the keyed ``kirocrew_sb_<pid>_`` shape. ``matcher`` lets the
    # legacy-residue sweep reuse this traversal — and, critically, its coverage
    # accounting (zombie leaders via task/, the vanish re-listing, the
    # foreign-uid forgiveness) — for a different name shape, instead of a
    # second scan that gets those cases subtly wrong.
    match = matcher or (lambda name: name.startswith(_MOUNT_SOURCE_PREFIX))
    # Fast pre-filter, applied to the whole table before any line is split;
    # only valid for the default shape, since a custom matcher may accept
    # names without the prefix.
    line_hint = _MOUNT_SOURCE_PREFIX.encode() if matcher is None else None
    # Mount tables already parsed this scan, by their exact bytes. Every
    # thread of a group shares its leader's mount namespace unless it
    # ``unshare``d one, so the thousands of sibling reads the coverage
    # accounting requires are near-duplicates of a few dozen distinct tables:
    # measured 12.5k tasks, 1.3M mountinfo lines, 18 distinct tables on one
    # host. Identical bytes contribute identical pins, so a table is split
    # into lines and matched once; a repeat costs the read alone (which
    # releases the GIL) and no Python-level per-line work. The read itself is
    # still issued for every task, so the OSError each caller keys its
    # coverage accounting on is unaffected.
    # Cache only a bounded number and volume of distinct tables. Once either
    # limit is reached, tables already present still deduplicate while every
    # uncached table is parsed directly without being retained.
    parsed_tables: set[bytes] = set()
    parsed_table_bytes = 0
    cache_full = False

    def _collect(mountinfo_path: str) -> None:
        """Add every matching bind SOURCE named in one mountinfo to ``pinned``.

        Propagates ``OSError`` exactly as ``open`` and a read would, so each
        caller decides what an unreadable task means for coverage. A source
        removed while still bound reads ``.../name//deleted``; the suffix is
        stripped so the real name is what pins.
        """
        nonlocal cache_full, parsed_table_bytes
        with open(mountinfo_path, "rb") as fh:
            data = fh.read()
        if line_hint is not None and line_hint not in data:
            return
        if data in parsed_tables:
            return
        for line in data.decode("utf-8", errors="replace").splitlines():
            if line_hint is not None and _MOUNT_SOURCE_PREFIX not in line:
                continue
            fields = line.split()
            if len(fields) > 3:
                source = fields[3]
                if source.endswith("//deleted"):
                    source = source[: -len("//deleted")]
                source = os.path.basename(source)
                if match(source):
                    pinned.add(source)
        if not cache_full:
            if (
                len(parsed_tables) >= _MOUNT_TABLE_CACHE_MAX_ENTRIES
                or parsed_table_bytes + len(data) > _MOUNT_TABLE_CACHE_MAX_BYTES
            ):
                cache_full = True
            else:
                parsed_tables.add(data)
                parsed_table_bytes += len(data)

    # Coverage accounting for ``coverage``. A task of this uid (or the overflow
    # uid) that could not be read is never re-read (it is in ``seen``), so it
    # clears coverage for good. One that departed between a pass's listing and
    # its read may have handed its namespace to a child forked after that
    # listing: a re-listing shows the child, so a departure on a pass that IS
    # followed by another pass is accounted for, and only the final pass's
    # departures reach the caller.
    unread_sticky = False
    departed_this_pass = False
    uids: dict[str, int | None] = {}

    def _could_be_descendant(name: str) -> bool:
        # A descendant stats as this uid, or as the overflow uid from inside a
        # nested user namespace; root can ``nsenter`` ANY namespace, so a root
        # task counts too. Without a uid to compare against (no ``os.getuid``;
        # an unreadable overflowuid sysctl) every task might be one, so
        # coverage fails closed rather than open.
        uid = uids.get(name)
        if own_uid is None or overflow_uid is None or uid is None:
            return True
        return uid == own_uid or uid == overflow_uid or uid == 0

    def _report() -> None:
        if coverage is not None and (unread_sticky or departed_this_pass):
            coverage.covered = False

    for _ in range(_PIN_SCAN_MAX_PASSES):
        try:
            listed = [n for n in os.listdir(proc_root) if n.isdecimal()]
        except OSError:
            if coverage is not None:
                coverage.covered = False
            return pinned, False
        if "1" not in listed and "1" not in seen:
            # Filtered procfs (hidepid/subset): coverage is unprovable, but the
            # pids that ARE listed — this uid's own, which is where every
            # sandbox descendant lives — still read fine and still pin. Keep
            # walking so their pins reach the caller: a visible holder retains
            # its source. Returning here instead would hand the gate an empty
            # pinned set. Coverage falls with the flag: root's tasks are among
            # the hidden ones, and root can hold any namespace.
            complete = False
            unread_sticky = True
        new_pids = [n for n in listed if n not in seen]
        vanished = False
        departed_this_pass = False
        # Learn every new task's uid FIRST, in one tight loop, so a task that
        # departs during the (much longer) mountinfo walk below can still be
        # told apart from another user's; one gone before even this read is
        # treated as possibly ours.
        uids = {name: _task_uid(proc_root, name) for name in new_pids}
        for name in new_pids:
            seen.add(name)
            try:
                _collect(os.path.join(proc_root, name, "mountinfo"))
            except (FileNotFoundError, ProcessLookupError):
                # May have handed its namespace to a child forked before the
                # exit — visible to the next listing, so take another pass.
                vanished = True
                if _could_be_descendant(name):
                    departed_this_pass = True
            except OSError as exc:
                if exc.errno == errno.EINVAL:
                    # EINVAL says THIS TASK's nsproxy is gone, so this task is a
                    # member of no mount namespace. That is NOT yet a statement
                    # about the namespace: a thread-group leader can exit through
                    # ``pthread_exit`` while sibling THREADS keep running, and
                    # threads share the nsproxy, so the namespace — and its binds
                    # on our sources — can still be alive behind a zombie leader.
                    # ``proc_root`` lists only thread-group LEADERS, so the
                    # re-listing below can never see those threads. Measured on a
                    # real zombie leader: ``/proc/<tgid>/mountinfo`` is EINVAL
                    # while ``/proc/<tgid>/task/<live-tid>/mountinfo`` reads fine.
                    # So ask the group before concluding anything; one readable
                    # thread yields the WHOLE namespace's mount table, because
                    # every thread in the group shares it.
                    task_dir = os.path.join(proc_root, name, "task")
                    try:
                        tids = os.listdir(task_dir)
                    except FileNotFoundError:
                        tids = []  # the group is gone entirely
                    except OSError:
                        complete = False  # cannot ask — coverage unprovable
                        if _could_be_descendant(name):
                            unread_sticky = True
                        continue
                    unaccounted = 0
                    for tid in tids:
                        if tid == name:
                            continue  # the leader: it already answered EINVAL
                        try:
                            _collect(os.path.join(task_dir, tid, "mountinfo"))
                        except OSError:
                            unaccounted += 1
                    # INVARIANT, and there is deliberately no third case: a sibling
                    # that READS contributes pins and nothing else, and a sibling
                    # that does not read, for ANY reason, makes coverage unprovable.
                    #
                    # Departed siblings are counted too, rather than excused. One
                    # that exits between the ``tids`` snapshot and its read may have
                    # spawned a successor THREAD first, and a successor thread is
                    # invisible to the outer re-listing, which enumerates
                    # thread-group LEADERS only — so no re-listing can recover it
                    # and only fail-closed is honest. The cost is transient, never a
                    # stall: a sibling caught mid-exit is a race, so the next sweep
                    # sees a settled group, whereas the zombie LEADER this branch
                    # exists for is a steady state and stays reclaimable.
                    #
                    # Tasks in one group can also hold DIFFERENT mount namespaces (a
                    # thread may ``unshare(CLONE_NEWNS)``, and the launcher's own
                    # user namespace grants its descendants the CAP_SYS_ADMIN that
                    # needs), so a readable sibling never speaks for an unreadable
                    # one.
                    if unaccounted:
                        complete = False
                        if _could_be_descendant(name):
                            unread_sticky = True
                        continue
                    # Otherwise this is a VANISH, unconditionally. Reaching this
                    # branch at all means the LEADER departed, and a departing task
                    # may have handed its namespace to a process forked after this
                    # pass's listing, which only a re-listing can see — so a
                    # sibling that could be read must not be able to cancel it:
                    # the pins it yields are kept, its success is not evidence.
                    #
                    # This terminates: the pid enters ``seen`` above and is never
                    # re-read, so a stable zombie costs exactly one extra pass,
                    # while genuine churn exhausts ``_PIN_SCAN_MAX_PASSES`` and
                    # returns complete=False, which retains rather than removes.
                    vanished = True
                    if _could_be_descendant(name):
                        departed_this_pass = True
                    continue
                try:
                    st_uid: int | None = os.stat(os.path.join(proc_root, name)).st_uid
                except OSError:
                    st_uid = None
                if (
                    own_uid is None
                    or st_uid is None
                    or overflow_uid is None
                    or st_uid == own_uid
                    or st_uid == 0
                    or st_uid == overflow_uid
                ):
                    complete = False
                    # Root's or another user's unreadable task is a host-wide
                    # gap only; one that could be a launcher descendant also
                    # clears the narrower coverage the gate relies on.
                    if _could_be_descendant(name):
                        unread_sticky = True
            else:
                # The leader read. Its THREADS may still hold OTHER mount
                # namespaces -- a thread can ``unshare(CLONE_FS)`` and then
                # ``setns`` into a sandbox's namespace while its leader stays
                # outside, and root has the capability to do so anywhere -- and
                # ``proc_root`` lists leaders only, so those namespaces are
                # reachable through ``task/`` alone. Asked only for a possible
                # holder (another user's threads cannot enter a namespace this
                # uid staged), which keeps the cost to this uid's and root's
                # groups: measured 3.5k sibling reads in 0.13s on a dev host.
                if not _could_be_descendant(name):
                    continue
                task_dir = os.path.join(proc_root, name, "task")
                try:
                    tids = os.listdir(task_dir)
                except FileNotFoundError:
                    # The group went away right after the leader read: a
                    # departure, with the same successor concern as any other.
                    vanished = True
                    departed_this_pass = True
                    continue
                except OSError:
                    complete = False
                    unread_sticky = True
                    continue
                departed = unreadable = False
                for tid in tids:
                    if tid == name:
                        continue
                    try:
                        _collect(os.path.join(task_dir, tid, "mountinfo"))
                    except (FileNotFoundError, ProcessLookupError):
                        departed = True
                    except OSError as exc:
                        if exc.errno == errno.EINVAL:
                            continue  # a thread mid-exit: it holds no namespace
                        unreadable = True
                if unreadable:
                    complete = False
                    unread_sticky = True
                elif departed:
                    # A thread that exited between the ``tids`` snapshot and its
                    # read may have left a successor THREAD holding its
                    # namespace, which the outer re-listing (leaders only)
                    # cannot show -- so the GROUP is re-read on the next pass
                    # rather than this being either excused or made sticky.
                    seen.discard(name)
                    vanished = True
                    departed_this_pass = True
        if not vanished:
            # Every departure so far was followed by a re-listing, so what
            # remains unaccounted for is the still-present tasks that could not
            # be read (sticky) plus nothing from this pass's departures.
            _report()
            return pinned, complete
    # Still churning after the pass budget — coverage unproven, and the final
    # pass's departures had no re-listing to catch a successor.
    _report()
    return pinned, False


#: Wall-clock budget for ONE reclaim pass, keyed and legacy alike. A pile that
#: cannot be cleared inside it is left for the next pass: the sweep runs on the
#: maintenance executor, whose threads other housekeeping shares, and reclaim is
#: resumable by construction (each entry is decided independently, and nothing
#: depends on a pass finishing). Measured for scale: 939k directories took ~11s
#: on tmpfs, so this clears a normal backlog in one pass and spreads a
#: pathological one over a few, instead of occupying a worker for a minute.
_SWEEP_TIME_BUDGET_SECONDS = 10.0

#: How often the budget is consulted. A clock read per entry would be a
#: measurable share of the work at these counts; per batch is close enough for a
#: budget whose only job is to bound the pass.
_SWEEP_BUDGET_CHECK_EVERY = 4096


def _cleanup_stale_sandbox_mount_sources(*, roots: Sequence[str] | None = None) -> int:
    """Reclaim orphaned bind-mount sources staged by the namespace launcher.

    The launcher stages one empty dir per sensitive_dirs entry, one empty file
    per sensitive_files entry, and (strict only) an SSH shadow dir holding a
    known-hosts copy, all named ``kirocrew_sb_<pid>_*`` on a tmpfs root. The
    kernel pins each source while its bind-mount lives, so the launcher cannot
    remove them and they orphan when the sandboxed process exits. Left alone
    they exhaust the runtime tmpfs (``/run/user/$UID``), at which point the
    systemd user manager cannot allocate transient scope units and every
    ``systemd-run --scope``-wrapped spawn fails.

    An entry becomes a reclaim candidate when its embedded pid is dead, or
    past ``_MOUNT_SOURCE_MAX_AGE_SECONDS`` (the backstop for a pid recycled
    onto an unrelated live process — routine on a ``pid_max=32768`` host).
    What a candidate's removal may touch is then decided by kind, because a
    bind source is NOT protected by the kernel against the sweeper: removing
    it succeeds from this namespace (mountinfo then shows ``//deleted``), a
    removed source DIRECTORY leaves the live mount's root inode ``S_DEAD`` so
    every create under the masked path fails from then on, and a non-empty
    dir's contents are visible inside any namespace still binding it:

    - Plain files: ``os.remove``. A file source's inode is held by the mount
      like an open descriptor, so the masked view is unaffected.
    - Dirs, empty or not: removed only when no readable mount namespace
      references the entry (:func:`_mount_pinned_source_names`) AND absence
      of a pin is positively established — by the scan proving it covered
      every namespace on the host, OR by it having read every task that
      could hold one (:class:`_PinScanCoverage`: this uid's, the overflow
      uid's and root's tasks, none unreadable, none departed between the
      final listing and its read). The second is what this fix adds. The
      host-wide flag also drops for another user's unreadable or departing
      task, which cannot be holding a source this uid staged (the sandboxed
      child keeps this uid with NO_NEW_PRIVS; only root can enter a foreign
      namespace), and requiring it alone is what stranded the directory
      class permanently on the hosts this sweep exists for (observed:
      929,540 dirs retained against 511 files reclaimed, the runtime tmpfs
      back at 100% of its inodes and every spawn failing again). A recycled
      pid reads live yet has no mount, so its entry is still reclaimed after
      the age backstop rather than stranded, while a genuine long-lived
      sandbox is pinned by its own process. A namespace no PROCESS holds has
      no mountinfo to report a pin — an fd- or bind-pinned namespace with
      zero members is out of scope, and the launcher never creates one — so
      the pin set cannot go stale in the deleting direction. The
      fresh-and-alive skip above stays load-bearing for the launcher's own
      staging window (after ``mkdtemp``, before ``mount``), when its entries
      are legitimately live and not yet pinned.

    Deliberately conservative about names: only the recognized
    ``kirocrew_sb_<pid>_`` shape with an ASCII positive pid is touched.
    Foreign ``tmp*`` names carry no liveness key and are left alone, as are
    ``kirocrew_sandbox_*`` launcher scripts, the ``kirocrew_sbprobe_*`` tmpfs
    probe, and prefix matches whose segment is not an ASCII positive decimal
    (an oversized all-digit segment IS the recognized shape — it probes
    OverflowError, reads stale, and is reclaimed). Some of these roots are
    world-writable, so a planted entry — including a deep tree built to make
    ``rmtree`` recurse — must fail toward "skip", never toward an exception
    that kills the sweep.

    Returns:
        Number of entries removed.
    """
    now = time.time()
    started = time.monotonic()
    examined = 0
    budget_spent = False
    if roots is None:
        roots = _mount_source_candidate_roots()
    # (pinned set, scan-was-complete) — built lazily, once, on the first
    # directory candidate (empty ones included: rmdir is gated too).
    pin_scan: tuple[set[str], bool] | None = None
    # Whether that scan read every task that could be a launcher descendant
    # (``_PinScanCoverage``) -- the narrower claim the gate accepts when the
    # host-wide flag is down for reasons that cannot involve a sandbox.
    coverage = _PinScanCoverage()
    removed = 0
    dirs_held_back = 0
    for root in roots:
        if budget_spent:
            break
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        for entry in entries:
            if not entry.startswith(_MOUNT_SOURCE_PREFIX):
                continue
            examined += 1
            if (
                examined % _SWEEP_BUDGET_CHECK_EVERY == 0
                and (time.monotonic() - started) > _SWEEP_TIME_BUDGET_SECONDS
            ):
                # Out of budget: stop cleanly and let the next pass continue.
                # Reclaim is resumable per entry, so a partial pass is progress,
                # never an inconsistent state.
                budget_spent = True
                break
            pid_str, sep, _rest = entry[len(_MOUNT_SOURCE_PREFIX) :].partition("_")
            pid = _parse_pid_segment(pid_str) if sep else None
            if pid is None:
                continue  # foreign / probe / planted names — no liveness key
            path = os.path.join(root, entry)
            try:
                mtime = os.lstat(path).st_mtime
            except OSError:
                continue
            over_age = (now - mtime) > _MOUNT_SOURCE_MAX_AGE_SECONDS
            # Liveness probe via the shim — NEVER raw os.kill(pid, 0), which
            # TERMINATES the target on Windows (platform_compat).
            try:
                alive = platform_compat.pid_exists(pid)
            except OverflowError:
                alive = False  # absurd pid digits from a corrupt name — stale
            if alive and not over_age:
                continue
            if os.path.isdir(path) and not os.path.islink(path):
                # ANY dir removal — even rmdir of an empty one — S_DEADs a
                # live mount's root inode, so absence of a pin must be
                # POSITIVELY established: either the host-wide scan proved its
                # coverage, or it read every task that could be a launcher
                # descendant (``coverage.covered``). The second is what keeps
                # another user's unreadable or departing task from retaining
                # the class forever — it cannot be holding a source this uid
                # staged.
                if pin_scan is None:
                    pin_scan = _mount_pinned_source_names(coverage=coverage)
                pinned, scan_complete = pin_scan
                if entry in pinned or not (scan_complete or coverage.covered):
                    dirs_held_back += 1
                    continue
                try:
                    os.rmdir(path)
                    removed += 1
                    continue
                except OSError:
                    pass
                try:
                    shutil.rmtree(path, ignore_errors=True)
                except Exception:
                    continue  # e.g. a planted tree deep enough to exhaust recursion
                # ignore_errors swallows a partial failure — count only a
                # confirmed removal.
                if not os.path.lexists(path):
                    removed += 1
            else:
                try:
                    os.remove(path)
                    removed += 1
                except OSError:
                    pass
    if dirs_held_back:
        # An always-closed pin scan is otherwise indistinguishable from a
        # working one while the dominant (directory) leak class re-accumulates
        # — surface it so an operator can tell retention from reclamation.
        scan_complete = pin_scan is not None and pin_scan[1]
        covered = scan_complete or coverage.covered
        # WARNING for the incomplete case, INFO for the benign pinned one.
        # Holding entries back because a live namespace binds them is normal
        # operation; holding them back on an unprovable scan is a FAULT — now
        # survivable, because a candidate whose staging group is gone is
        # reclaimed anyway, so what remains here is confined to entries whose
        # tree still looks alive. Coverage of every task that could be a
        # launcher descendant counts as proven for this purpose.
        (logger.info if covered else logger.warning)(
            "sandbox mount-source sweep: %d dir candidate(s) held back (%s)",
            dirs_held_back,
            (
                "pinned by a live mount namespace"
                if covered
                else "pin scan incomplete and descendant coverage unproven"
            ),
        )
    if budget_spent:
        logger.info(
            "sandbox mount-source sweep: paused at the %.0fs budget after %d entries "
            "(%d reclaimed this pass); the next pass resumes",
            _SWEEP_TIME_BUDGET_SECONDS,
            examined,
            removed,
        )
    return removed


#: A bind-mount source staged with ``tempfile``'s DEFAULT names carries no pid,
#: so the pid-keyed sweep above cannot reason about it at all. A host holding
#: such entries holds them permanently — 1,836,596 entries measured on one host,
#: enough on its own to exhaust the runtime tmpfs's inodes and stop every
#: ``systemd-run --scope``-wrapped spawn. This is ``tempfile``'s exact shape:
#: the ``tmp`` prefix plus 8 characters of its own alphabet.
_LEGACY_MOUNT_SOURCE_RE = re.compile(r"^tmp[a-z0-9_]{8}$")

#: How many legacy-shaped candidates a root must hold before the legacy pass
#: touches ANY of them. An unkeyed name carries no provenance, so the pile IS
#: the provenance: no program's ordinary scratch use leaves dozens of empty,
#: 0o700, day-old ``tmp*`` directories in the session runtime dir, whereas the
#: leak this pass exists for left 1.8 million on one host. Below the threshold
#: every candidate is retained and the pass retires — there is no pile to heal.
_LEGACY_PILE_THRESHOLD = 64

#: Written once a legacy pass has completed, so the scan is not repeated for the
#: life of the install: no current build creates these names, so a completed
#: pass is final.
_LEGACY_RESIDUE_MARKER = ".legacy-mount-source-residue-swept"


def _launcher_tmpfs_roots() -> list[str]:
    """The root the legacy pass may walk: the session runtime dir alone.

    Deliberately narrower than :func:`_mount_source_candidate_roots`. The legacy
    sweep matches an unkeyed ``tmp*`` name, so it may only walk a root where
    that shape is far more likely ours than a stranger's. ``/run/user/$UID`` is
    the launcher's first pick, is scoped to this uid's login session (nothing
    keeps durable state there), and is where the observed pile lived.
    ``/dev/shm`` is NOT walked even though the launcher falls back to it: it is
    a host-wide tmpfs where any same-uid program's ``tempfile`` scratch
    legitimately lives, and an empty day-old scratch dir there can still be a
    live program's — a host whose launcher fell back to ``/dev/shm`` keeps the
    manual note in the issue instead. The shared system tempdir is excluded
    for the same reason, more so.
    """
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return []
    return [f"/run/user/{getuid()}"]


def _bound_source_basenames(
    proc_root: str = "/proc", *, coverage: _PinScanCoverage | None = None
) -> tuple[set[str], bool]:
    """Basenames of legacy-shaped bind sources named by any live mount, plus
    whether coverage was positively established.

    The same traversal as :func:`_mount_pinned_source_names` — zombie leaders
    consulted through ``task/``, the vanish re-listing, foreign-uid forgiveness
    — with the name predicate swapped for tempfile's shape, because the legacy
    residue carries no recognizable prefix. A first cut re-implemented the walk
    and got two things wrong that this delegation cannot: it filtered on the
    root field's DIRNAME, which is the source's path *within its own
    filesystem* (``/tmpab12cd34``, never ``/run/user/$UID/tmpab12cd34``) so the
    fence silently matched nothing; and it reported every ``EINVAL`` as a
    coverage gap, which on a real host with a couple of dozen zombie leaders
    made the pass permanently inert.

    Keyed by basename only, which is why no root is taken: a same-named entry
    on another filesystem shares the pin, which errs toward retention.
    """
    return _mount_pinned_source_names(
        proc_root,
        matcher=lambda name: bool(_LEGACY_MOUNT_SOURCE_RE.match(name)),
        coverage=coverage,
    )


def _cleanup_legacy_mount_source_residue(data_home: Path) -> int:
    """One-shot reclaim of the legacy, pid-less bind-mount source residue.

    The pid-keyed sweep can never touch an entry that carries no pid, so a host
    holding that pile keeps the runtime tmpfs at its inode ceiling and every
    agent spawn keeps failing. Runs from the same entry point as the keyed sweep,
    so the gateway's first cleanup pass heals the host without an operator ever
    learning what ``Failed to start transient scope unit: No space left on
    device`` meant.

    An unkeyed name cannot be PROVEN to be ours, so every fence here is about
    keeping a stranger's entry rather than reclaiming ours:

    - only on the session runtime tmpfs the launcher picks first
      (:func:`_launcher_tmpfs_roots`), never ``/dev/shm`` or the shared system
      tempdir, where another same-uid program's ``tempfile`` scratch lives —
      and when no such root is PRESENT the pass returns before the pin scan,
      because there is nowhere for an entry of this class to be;
    - only ``tempfile``'s exact shape (:data:`_LEGACY_MOUNT_SOURCE_RE`);
    - only DIRECTORIES owned by THIS uid with the exact mode ``mkdtemp``
      creates (0o700) — a hand-made or umask-shaped entry is not one of
      these, and the old build's ``mkstemp`` file sources are left alone: an
      unlinked file another program still holds open loses what it writes
      next, which no fence here can rule out, while an empty dir holds
      nothing to lose;
    - only past ``_MOUNT_SOURCE_MAX_AGE_SECONDS``, which every real member of
      this class is by construction (nothing creates the shape now) while a
      live program's scratch dir usually is not;
    - only when no live mount names the entry as its source, and only when
      that absence was POSITIVELY established (:func:`_bound_source_basenames`
      complete, or every possible holder read — the same two claims as the
      keyed dir gate, since ``/run/user/$UID`` is reachable by this uid and
      root alone);
    - only once a root shows the PILE this pass exists for
      (:data:`_LEGACY_PILE_THRESHOLD` candidates passing every fence above):
      a stray scratch dir or two never trips it and is retained outright,
      while the leak class arrives by the hundred thousand;
    - dirs go through ``os.rmdir``, which REFUSES a non-empty directory: that
      is the emptiness fence, so a populated stranger's dir survives without a
      listing, and so does the legacy SSH shadow dir (it holds a known-hosts
      copy) — left for the human note in the issue rather than removed here;
    - the one-shot marker is stamped only by a pass that reached the end AND
      found nothing retained for age alone, so a host upgrading within a day
      of its last old-build spawn does not retire the pass on that cohort.

    Returns:
        Number of entries removed.
    """
    marker = data_home / _LEGACY_RESIDUE_MARKER
    try:
        if marker.exists():
            return 0
    except OSError:
        return 0
    roots = [root for root in _launcher_tmpfs_roots() if os.path.isdir(root)]
    if not roots:
        # Nothing this pass may walk is present, so no entry of this class can
        # be here to reclaim: every host off Linux (``/run/user/$UID`` is a
        # logind construct), and a Linux host whose session runtime dir is
        # absent. Return BEFORE the pin scan, not after it: that scan reads
        # ``/proc``, which off Linux does not exist, so the coverage claim below
        # can never be established there and the pass reported an unprovable
        # scan at WARNING on every tick — for a root it was never going to
        # walk. The marker is deliberately NOT stamped: this branch verified no
        # residue, it merely found nowhere to look, and a repeat pass now costs
        # one stat, so retiring the pass here would trade a free no-op for a
        # claim it cannot make.
        return 0
    coverage = _PinScanCoverage()
    bound, complete = _bound_source_basenames(coverage=coverage)
    if not (complete or coverage.covered):
        # Absence-of-bind not established — retry on the next sweep rather than
        # remove on the strength of an unproven scan, and do NOT stamp the
        # marker, or one bad scan would retire the pass forever. The same two
        # claims the keyed dir gate accepts: ``/run/user/$UID`` is 0o700 and
        # this uid's, so its entries are reachable by this uid and root alone,
        # which is exactly what ``covered`` vouches for. Say so at WARNING,
        # like the keyed sweep's held-back report: on a host whose /proc never
        # settles this pass stays inert every tick, and a silent return would
        # be the same invisible-at-default-log-level failure the keyed sweep's
        # diagnostic exists to end.
        logger.warning(
            "sandbox mount-source sweep: legacy pass retained everything — bind "
            "coverage of /proc could not be established (pin scan incomplete, "
            "holder coverage unproven); the pass retries on the next tick"
        )
        return 0
    now = time.time()
    started = time.monotonic()
    examined = 0
    budget_spent = False
    getuid = getattr(os, "getuid", None)
    own_uid = getuid() if getuid is not None else None
    removed = 0

    young_retained = False

    def _reclaim(path: str) -> bool:
        try:
            os.rmdir(path)  # refuses a non-empty dir by design
        except OSError:
            return False
        return True

    for root in roots:
        if budget_spent:
            break
        try:
            entries = os.scandir(root)
        except OSError:
            continue
        # Candidates are buffered until the root proves it holds the pile;
        # below the threshold nothing in the buffer is touched. Once it is
        # reached the buffer is drained and reclaim streams from then on.
        pending: list[str] = []
        engaged = False
        with entries:
            for entry in entries:
                if not _LEGACY_MOUNT_SOURCE_RE.match(entry.name) or entry.name in bound:
                    continue
                examined += 1
                if (
                    examined % _SWEEP_BUDGET_CHECK_EVERY == 0
                    and (time.monotonic() - started) > _SWEEP_TIME_BUDGET_SECONDS
                ):
                    budget_spent = True
                    break
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if own_uid is not None and info.st_uid != own_uid:
                    continue
                # Directories only. The old build staged a mkstemp FILE per
                # masked file too, but an unlinked file another program still
                # holds open loses whatever it writes next, and nothing here
                # can tell such a file from ours; an empty directory holds no
                # data to lose and a stranger's mkdtemp dir gets ENOENT on its
                # next create, an error rather than silent loss.
                if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
                    continue
                if (now - info.st_mtime) <= _MOUNT_SOURCE_MAX_AGE_SECONDS:
                    young_retained = True  # ages into candidacy on a later pass
                    continue
                if engaged:
                    removed += _reclaim(entry.path)
                    continue
                pending.append(entry.path)
                if len(pending) >= _LEGACY_PILE_THRESHOLD:
                    engaged = True
                    for queued in pending:
                        removed += _reclaim(queued)
                    pending = []
        if pending and not engaged:
            logger.info(
                "sandbox mount-source sweep: %d legacy-shaped entries under %s are below "
                "the pile threshold (%d); retained as not provably ours",
                len(pending),
                root,
                _LEGACY_PILE_THRESHOLD,
            )
    if budget_spent:
        logger.info(
            "sandbox mount-source sweep: legacy pass paused at the %.0fs budget after "
            "%d entries (%d reclaimed); the next pass resumes",
            _SWEEP_TIME_BUDGET_SECONDS,
            examined,
            removed,
        )
    elif young_retained:
        logger.info(
            "sandbox mount-source sweep: legacy pass complete but not retired — "
            "legacy-shaped entries under the age fence remain; the next pass "
            "that finds none stamps the marker"
        )
    else:
        # Stamp only a pass that reached the end of the residue AND left no
        # cohort behind for age, or a budget-truncated walk's remainder — or a
        # host upgrading within a day of its last old-build spawn — would be
        # retired unswept.
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            # Create-only and never through a symlink: a planted link at this
            # path must fail the stamp (the pass just repeats) rather than
            # have the gateway write wherever it points.
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, f"{int(now)} removed={removed}\n".encode())
            finally:
                os.close(fd)
        except OSError:
            # An unstampable marker only costs a repeat pass, which is idempotent.
            logger.debug("legacy mount-source sweep: could not stamp %s", marker, exc_info=True)
    if removed:
        logger.info(
            "sandbox mount-source sweep: reclaimed %d pre-prefix legacy entries "
            "(one-shot; these carry no pid and no earlier build could reclaim them)",
            removed,
        )
    return removed


def _cleanup_retired_acp_snapshot_dir(data_home: Path) -> int:
    """Reclaim `<config_dir>/run/kiro-cli-snapshots` from before the in-place launch.

    The CLI is launched in place, so nothing writes this tree, and a host that
    holds per-ACP-generation copies of the kiro-cli binary here keeps every
    orphaned copy forever (observed: 196 MB in two ~100 MB copies). Nothing else
    reclaims them:
    `kiro_crew.sandbox.cleanup_stale_sandbox_profiles`'s own sweep only matches
    `kirocrew_sandbox_*` FILES in this same dir, and
    the tree sits on `security._SENSITIVE_HOME_DIRS`, so the agent cannot delete
    it either — on request or otherwise. Best-effort and idempotent; returns 1
    when a tree was removed so the periodic sweep logs it.
    """

    retired = data_home / "run" / "kiro-cli-snapshots"
    if not retired.is_dir():
        return 0
    shutil.rmtree(retired, ignore_errors=True)
    # ignore_errors swallows partial failures, so report only a real removal.
    return 0 if retired.exists() else 1
