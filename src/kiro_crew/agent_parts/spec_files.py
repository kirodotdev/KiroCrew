"""How a spec file reaches disk: the write, its notification and the spec lock.

The foreign-spec refusal and the owner-derived write, the atomic JSON write and the
config notification it sends, the cross-process spec lock, and the agents directory
with the specs every install needs. New work on how a spec file is written goes here.

Composed onto ``kiro_crew.agent``, the import path and patch target of every name
here; see :mod:`kiro_crew.agent_parts`.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from kiro_crew.agent import (
        REQUIRED_KIRO_AGENT_FILES,
        SharedAgentHomeRefused,
        _audit_allowed_shared_write,
        _decline_shared_agent_home,
        _mc_config_path,
        _rebuild_spec_install_admitted,
        _warn_declined_home_once,
        kiro_agents_dir,
        logger,
        platform_compat,
        replace_with_retry,
        sel,
    )


def _declined_foreign_spec_write(path: Path) -> bool:
    """Is *path* a spec in an agents directory this instance must not own?

    ``rebuild_agent_config`` consults :func:`_decline_shared_agent_home` and
    returns without writing when the answer is yes. The DERIVED specs had no
    such check: ``kirocrew-worker.json`` (``worker_agent``), the conductor
    agents, the service agents (guest, lite, knowledge, research) and the
    read-only side spec are each written straight through
    :func:`_atomic_json_write`, and most of them on DISPATCH rather than at
    boot. A second gateway on its own ``KIROCREW_HOME`` therefore re-pinned the
    machine-wide derived specs to its own ``.venv`` and data home every time it
    dispatched a worker — the exact poisoning the template-spec guard exists to
    prevent, arriving through a door the guard never covered.

    Checked HERE, at the primitive those derived writers share, rather than in
    each writer: a new materialization path that reaches this primitive is
    covered the day it is added, and such a writer cannot forget the check. It
    is NOT every spec write in the tree — ``apps/bridges_agents.py`` renders app agent
    specs into the same directory through :func:`atomic_write`, a different
    primitive this guard never sees; that writer, the auto_improvement runner's
    self-registration call this function themselves, under
    :func:`agents_spec_lock`. What this covers on its own is the set that goes
    through
    :func:`_atomic_json_write`: ``worker_agent``, the conductor agents, the
    service agents, the default-spec commit, the fork refresh and the read-only
    side spec. The decision itself is not re-implemented — it is the same
    :func:`_decline_shared_agent_home` the rebuild asks, so the two cannot
    drift and every exemption that function grants (a private redirected
    target, the dedicated ``isolated_agents_dir(own home)``, a default-home
    instance, an instance whose own provenance is in the specs) is honoured
    identically.

    Scoped to the agents directory by comparing the write's PARENT against
    :func:`kiro_agents_dir_path`, so this primitive's other callers — notably a
    ``config.json`` write — are untouched. ``audit=False`` is passed to the
    shared decision so it does not emit the rebuild's own boot-path event under
    a source that did not make this call; both halves are recorded here instead,
    under this source. The REFUSAL is recorded via the ``denied`` branch below;
    an ADMITTED write that is not inside an already-admitted rebuild is recorded
    via :func:`_audit_allowed_shared_write`. EVERY admitted shared-home write is
    audited this way, including on a single-gateway (default-home) install -- the
    permission grant for a per-dispatch write made outside the boot rebuild has
    no other record, so leaving it unaudited would be a gap in the SEL trail. The
    one admitted case that stays silent is a PRIVATE redirected target, which is
    not a decision about the shared resource (the same reason
    :func:`_decline_shared_agent_home` does not audit its private-target
    returns). The rebuild's own grant is still audited once per rebuild and does
    not reach the allowed branch (the ``_rebuild_spec_install_admitted`` arm
    returns first), so there is no double-record. Neither half is throttled; the
    dedupe below covers the operator-facing WARNING only, which repeats ~1/min
    for the process lifetime.
    """
    try:
        if path.parent.resolve() != kiro_agents_dir_path().resolve():
            return False
    except OSError:
        # The parent could not be resolved, so we cannot PROVE this write lands
        # outside the shared agents directory. Fail closed: a write we cannot
        # clear is treated as a non-owned shared-home write and refused, rather
        # than admitted on the unproven assumption it is private.
        return True
    if _rebuild_spec_install_admitted.get():
        # Inside a rebuild the guard already admitted. Asking again per write
        # would answer a DIFFERENT question than the rebuild's, because the
        # rebuild's own first spec changes what the temp-checkout arm sees --
        # see ``_rebuild_spec_install_admitted``. The rebuild has already emitted
        # its own "allowed" agent_home_write event once, so emitting here too
        # would double-count the same admitted transaction.
        return False
    if _decline_shared_agent_home(audit=False) is None:
        # The write is admitted. Record the "allowed" half for the shared agents
        # directory, so a derived-spec write admitted OUTSIDE a rebuild (e.g. a
        # worker mirror re-derived on spawn after an app deregistration left it
        # stale) is not a silent gap in the SEL trail -- the rebuild audits its
        # own grant, but these per-dispatch writers reach this primitive on their
        # own. A PRIVATE redirected target is exempt, not a decision about the
        # shared resource, so it emits nothing (mirrors _decline_shared_agent_home,
        # which does not audit its private-target returns). Best-effort: a lost
        # audit record must never turn an allowed write into a failure.
        _audit_allowed_shared_write(path)
        return False
    _warn_declined_home_once(
        "derived-spec",
        path.parent,
        "Refusing to write the derived agent spec %s from a data home that does "
        "not own %s: the spec would pin this instance's venv and data home into "
        "a file the owning gateway's sessions load. The existing spec is left in "
        "place. To let this instance own its specs, remove the stale kirocrew*.json "
        "specs from that directory and restart.",
        path.name,
        path.parent,
    )
    try:
        sel().log_api_access(
            caller="system",
            operation="agent_home_write",
            outcome="denied",
            source="derived-spec",
            resources=str(path.parent),
            error="data home does not own the shared agent home; derived spec write refused",
        )
    except Exception:  # noqa: BLE001 — a lost record must not turn a refusal into a write
        logger.debug("SEL audit unavailable for a refused shared write", exc_info=True)
    return True


def write_owner_derived_spec(path: Path, data: dict) -> None:
    """Write a spec whose content carries nothing of THIS instance.

    A second entry point rather than a keyword on :func:`_atomic_json_write`,
    for two reasons. The cross-split shape ratchet in
    ``test_agent_refactor_contracts`` pins that primitive's signature, so a new
    parameter there is a red; and the claim belongs in the NAME a caller reads
    at its own call site, where the reviewer of that call can check it, rather
    than in a flag threaded through a general-purpose writer.

    The one caller is the Connections mint: it copies ONE server entry verbatim
    out of the owner's own on-disk spec into a uniquely named throwaway its
    manifest sweep reaps. Nothing of this instance is pinned into the shared
    agents directory by it, so the ownership guard must not refuse it --
    otherwise every OAuth Connect fails on a relocated or worktree-booted
    instance. Authorization is the writer's claim, made here, and deliberately
    NOT a filename the guard recognizes: a name is not evidence of who produced
    the bytes, so a guard trusting one would let any writer reach this
    directory by choosing the right name.
    """
    _atomic_json_write_unguarded(path, data)


def _atomic_json_write(path: Path, data: dict) -> None:
    """Write JSON atomically via tmp+rename to prevent read-of-partial-file.

    kiro-cli reads agent configs at spawn and set_mode.  Non-atomic writes
    (truncate-then-write) can deliver empty or partial JSON, crashing the
    ACP process with exit code 1.  rename() is atomic on Linux when source
    and destination are on the same filesystem.

    Raises :class:`SharedAgentHomeRefused` when
    :func:`_declined_foreign_spec_write` says this instance does not own the
    agents directory the spec would land in. RAISING rather than returning is
    the point: the remedy for the directory is still the rebuild's — leave
    whatever already worked in place, write nothing — but "wrote nothing" and
    "wrote it" must not be indistinguishable to the caller. Were the refusal
    silent, every caller would read it as a commit: the dashboard's spec editor
    would answer 200 on an edit it discarded, the read-only side spec would
    return a derived name whose file is absent, the Connections mint would hand
    kiro-cli an agent it never wrote, and the worker mirror would record itself
    as re-derived while still carrying grants revoked from ``kirocrew.json``.
    ``SharedAgentHomeRefused`` is an ``OSError``, so each of those callers
    reports it through the failure arm it already has.

    The signature stays ``-> None``: the verdict travels as an exception, which
    the callers that must act on it already catch, so the cross-split shape
    ratchet in ``test_agent_refactor_contracts`` keeps pinning this primitive
    unchanged.

    The rename goes through ``replace_with_retry`` because atomicity is not the
    only way that step fails. On Windows ``os.replace`` raises
    ``PermissionError`` while ANY other handle is open on either path, and a
    just-written temp file is exactly what an indexer or AV scanner opens —
    so a correct atomic write can still lose its payload for reasons unrelated
    to this caller. Here that surfaces as a failed spawn, since these are the
    configs kiro-cli reads. The helper is Windows-only and never sleeps on the
    event loop; ``ensure_agent_materialized`` reaches this from
    ``asyncio.to_thread``, so the retry applies on the path that matters.

    Uses mkstemp for a unique temp file per call so concurrent writers
    to the same path don't clobber each other's temp files.

    The result is owner-only on POSIX (``0o600`` for a new file; an existing
    file keeps its owner bits and loses every group/other bit). An agent spec
    is where the vault's secrets are projected for kiro-cli -- a pre-registered
    ``oauth.clientSecret``, remote ``headers`` credentials -- and kiro-cli runs
    as the same user, so no reader needs more. On Windows
    ``fchmod_safe`` is a no-op and the file inherits the directory's ACL.
    """
    if _declined_foreign_spec_write(path):
        raise SharedAgentHomeRefused(
            f"refusing to write {path.name}: this data home does not own {path.parent}"
        )
    _atomic_json_write_unguarded(path, data)


def _atomic_json_write_unguarded(path: Path, data: dict) -> None:
    """The atomic tmp+rename write itself, with no ownership question asked.

    Split out so the guarded primitive and :func:`write_owner_derived_spec`
    share one write path: two copies would drift, and the mode handling and
    the Windows rename retry below are exactly what must not be duplicated.
    Private and NOT a general escape hatch -- a new caller of this instead of
    the guarded primitive is how the guard gets bypassed by accident.
    """
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            try:
                mode = stat.S_IMODE(path.stat().st_mode) & 0o700
            except FileNotFoundError:
                mode = 0o600
            platform_compat.fchmod_safe(f.fileno(), mode)
            json.dump(data, f, indent=2)
            f.write("\n")
        replace_with_retry(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    _notify_if_config_write(path)


def _notify_if_config_write(path: Path) -> None:
    """Drop the loader cache and wake the config watcher when *path* is ``config.json``.

    This writer bypasses the loader's own writers, so a ``config.json`` write
    through it would otherwise be the one path a running gateway never
    hot-applies. No dashboard handler takes that path -- the per-channel savers,
    the STT PUT and the MCP gateway toggle go through ``update_config_locked``,
    and ``TestTheAtomicJsonWriteConfigFamilyIsRatcheted`` holds that population
    at zero -- so this is the safety net for a writer the ratchet does not see.
    Any other target (an agent spec) is untouched. Best-effort: a resolution
    error must not fail the write that already landed.
    """
    try:
        target = _mc_config_path()
        same = path == target or path.resolve() == target.resolve()
    except OSError:
        return
    if not same:
        return
    from kiro_crew.config import live, loader

    loader._invalidate_config_cache()
    live.notify_config_written()


@contextlib.contextmanager
def agents_spec_lock(agents_dir: Path) -> Iterator[None]:
    """Cross-process advisory lock serializing every template-spec write.

    One lock for the fork/publish endpoints, the agent-detail PATCH, and the
    background fork refresh: a read-modify-writer that skips it can interleave
    with any of the others and silently revert their write. Sidecar lockfile
    (not the spec's own fd) for the same reason update_config_locked uses one:
    atomic replace swaps the inode, so a lock on the spec fd would not
    serialize across the rename.

    Both failure modes are REPORTED here before they propagate, because several
    callers catch this as best-effort work at ``logger.debug``. Without a report
    at a level an operator sees, a gateway that skipped its agent-spec install
    reads in the log exactly like one that completed it. An unwritable lock path
    (a read-only ``~/.kiro/agents`` mount) refuses at ``os.open``, before any lock
    is attempted; ``platform_compat.file_lock`` bounds the acquire itself.
    """
    lock_path = agents_dir / ".kirocrew-agents.lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        # Naming the path AND the errno is the point: "Read-only file system" on
        # this specific path is what tells the operator to move KIRO_HOME, and it
        # is not something retrying can recover.
        logger.warning(
            "cannot open the agent-spec lock %s (%s) — agent-spec writes cannot be "
            "serialized, so this install is being skipped; point KIRO_HOME at a "
            "writable directory if the filesystem is read-only",
            lock_path,
            exc.strerror or exc,
        )
        raise
    try:
        with contextlib.ExitStack() as stack:
            # ``enter_context`` rather than a ``with`` around the yield, so this
            # ``except`` covers the ACQUIRE ALONE. A caller-body OSError (an
            # atomic spec write hitting ENOSPC, an unlink hitting EACCES) reaches
            # the same handler if the yield sits inside it, and would then be
            # logged as a lock problem -- sending an operator after a stuck holder
            # while the real fault is the disk or the permission.
            try:
                stack.enter_context(platform_compat.file_lock(fd, exclusive=True, wait=True))
            except OSError as exc:
                # The bounded-acquire refusal. A stuck holder calls for a
                # DIFFERENT operator action (find the process still holding it)
                # than an unwritable path, so it must not be reported with the
                # same remedy as above. BlockingIOError is a caller's own
                # non-waiting choice, not a fault to report.
                if not isinstance(exc, BlockingIOError):
                    logger.warning("agent-spec lock %s: %s", lock_path, exc)
                raise
            yield
    finally:
        os.close(fd)


# Resolved per call, never captured at import: an import-time binding freezes
# the data home and defeats pod isolation, the lazy legacy-home migration and
# test isolation. The name below is an opt-in override (None = live home) so
# existing monkeypatch call sites keep working. See config.md "Data Home";
# dashboard/handlers/usage_store.py is the reference implementation.
KIRO_AGENTS_DIR: Path | None = None


def kiro_agents_dir_path() -> Path:
    """Kiro agents directory, resolved against the live data home.

    Honors the :data:`KIRO_AGENTS_DIR` override hook when a caller (test/tooling)
    has set it; otherwise resolves live via :func:`kiro_agents_dir`.
    """
    return KIRO_AGENTS_DIR if KIRO_AGENTS_DIR is not None else kiro_agents_dir()


def missing_required_agent_specs() -> list[str]:
    """Return the :data:`REQUIRED_KIRO_AGENT_FILES` absent from the agents dir.

    A post-install verification, not a duplicate of the install: an empty result
    is the only proof that ``rebuild_agent_config`` actually left usable specs on
    disk. Raising is NOT enough on its own, because two non-raising paths also
    end with no spec written:

    * ``rebuild_agent_config`` mkdirs the agents directory as its first act, so a
      failure anywhere after that leaves a created-but-EMPTY directory — which
      reads as "installed" to anything that only checks the directory.
    * it also RETURNS EARLY when :func:`_decline_shared_agent_home` refuses to
      rewrite a shared agent home. Correct on a machine that already has specs
      (it protects the real install's MCP servers); fatal on one that does not,
      where there is nothing to fall back to.

    Checking the filesystem covers both, plus a spec deleted after install. The
    cost of NOT checking is that the first symptom is kiro-cli answering every
    ``session/set_mode`` with "Mode '<name>' not found" — one failed turn at a
    time, with nothing pointing at the install as the cause.
    """
    if _decline_shared_agent_home(audit=False) is not None:
        # This instance is not allowed to own these specs (a pod, or a gateway
        # booted from a linked git worktree), so their absence is not a defect it
        # can repair. Reporting them would put an unrepairable install behind a
        # full-screen gate whose only remedy declines every time. ``audit=False``
        # keeps this read out of the SEL log -- the audit records write DECISIONS,
        # and a status poll is not one.
        return []
    agents_dir = kiro_agents_dir_path()
    return [name for name in REQUIRED_KIRO_AGENT_FILES if not (agents_dir / name).is_file()]


def present_required_agent_specs() -> list[tuple[str, Path]]:
    """Return the :data:`REQUIRED_KIRO_AGENT_FILES` that DO exist, with paths.

    The counterpart to :func:`missing_required_agent_specs`, for the caller that
    needs to ask a question ABOUT a spec rather than about its absence — currently
    whether kiro-cli accepts it.

    Shares that function's ownership guard on purpose. An instance not allowed to
    own these specs (a pod, or a gateway booted from a linked git worktree) must
    not report on them either: it did not write them, cannot repair them, and its
    verdict would describe another install's files.
    """
    if _decline_shared_agent_home(audit=False) is not None:
        return []
    agents_dir = kiro_agents_dir_path()
    return [
        (name, agents_dir / name)
        for name in REQUIRED_KIRO_AGENT_FILES
        if (agents_dir / name).is_file()
    ]
