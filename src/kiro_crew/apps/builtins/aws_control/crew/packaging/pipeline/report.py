"""The machine-readable report beside ``--out``: its schema, its ownership, its publication.

The schema is written here and read back here, by the check that decides whether an existing
report is one this tool wrote. The report is published by exclusive hard link after the
bundle is promoted, so the capability that publish needs is probed before promotion.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from kiro_crew.atomic_write import read_json_or

from . import destination as _destination
from . import pinned as _pinned
from .contract import REPORT_VERSION, ExportRefused
from .pinned import _NOFOLLOW_READ_FLAGS
from .staging import _RUN_ID


def _refuse_unless_our_report(path: Path, out_dir: Path) -> None:
    """Refuse a file at the report path unless this tool wrote it.

    Absent is fine: the ordinary first build. A directory or a link is left to
    ``_write_nofollow``, which judges shape and reports it precisely. What this adds is the
    one case shape cannot answer -- a plain file that happens to have this name -- because
    truncating it is indistinguishable from rebuilding until you look inside.

    The name alone is not proof, which is the same lesson the plan-only directory check
    learned: a file called ``curation-plan.json`` was deleted on its name until the check
    started reading ``plan_version``.
    """
    if _pinned._is_redirecting_entry(path):
        # Judged BEFORE ``is_file()``, which follows the link and on Windows follows a
        # reparse point naming a share -- the outbound SMB probe, from a path derived from
        # --out. ``_write_nofollow`` refuses the link afterwards, so returning here hands it
        # the decision instead of reaching the network to make one.
        return
    if not path.is_file():
        return
    body = read_json_or(path, None, logger=logging.getLogger(__name__), what="export report")
    # Both fields, not just the version. ``report_version`` is a generic key: any unrelated
    # JSON that happens to carry ``"report_version": 1`` was accepted as this tool's own
    # output and truncated. ``bundle_dir`` is the report's claim about WHICH bundle it
    # describes, and this build is about to write out_dir, so a report that names a different
    # destination is not the one this build would be replacing -- whoever wrote it is not us.
    if (
        isinstance(body, dict)
        and body.get("report_version") == REPORT_VERSION
        and body.get("bundle_dir") == str(out_dir)
    ):
        return
    raise ExportRefused(
        f"{path} already exists and this build did not write it (it does not carry "
        f"report_version {REPORT_VERSION} naming bundle_dir {out_dir}). The path is derived "
        f"from --out by appending "
        f"'.smc-bundle.json', and writing the report would replace its contents. Move it, "
        f"or point --out elsewhere."
    )


@dataclass
class BuildReport:
    bundle_dir: Path
    digest: str
    skill_count: int
    mcp_servers: list[str]
    denied: list[dict]
    notes: list[str]


def _write_report_temp(
    report_tmp: Path,
    *,
    crew_name: str,
    bundle_dir: Path,
    digest: str,
    skill_count: int,
    mcp_servers: list[str],
    denied: list[dict],
) -> None:
    """Write the report to its run-id temp name; ``_publish_report`` installs it later.

    The fields ``_refuse_unless_our_report`` reads back to recognise this tool's report --
    ``report_version`` and ``bundle_dir`` -- are written here, beside that check.
    """
    _destination._write_nofollow(
        report_tmp,
        json.dumps(
            {
                "report_version": REPORT_VERSION,
                "crew_name": crew_name,
                "bundle_dir": str(bundle_dir),
                "digest": digest,
                "skill_count": skill_count,
                "mcp_servers": mcp_servers,
                "denied": denied,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        # Claim the run-id scratch name with O_CREAT|O_EXCL, not O_TRUNC: this is a name
        # this build creates fresh, so a file already there was NOT written by this build,
        # and truncating it would overwrite something this transaction did not create. The
        # exclusive open refuses instead, so the scratch name is a checked claim rather than
        # an assumed one -- the same no-replace discipline the publish and the aside use.
        exclusive=True,
    )


#: The errnos a filesystem raises when hard links are simply not supported there -- FAT/exFAT,
#: many network mounts, some overlay configurations. ``os.link`` reports one of these rather
#: than ``FileExistsError``, and every publish link in ``_publish_report`` treats a failure as
#: a race lost, so an unsupported-capability errno must be answered BEFORE promotion, not there.
_HARD_LINK_UNSUPPORTED_ERRNOS = frozenset(
    e for e in (getattr(errno, n, None) for n in ("EPERM", "EOPNOTSUPP", "ENOSYS", "EMLINK")) if e
)


def _refuse_report_dir_without_hard_link_support(report_path: Path) -> None:
    """Refuse, before promotion, when the report directory cannot do hard links.

    ``_publish_report`` installs the report by EXCLUSIVE HARD LINK (``os.link``) so a
    concurrent writer at the report path is ANSWERED by ``FileExistsError`` rather than
    clobbered. But ``os.link`` is a filesystem CAPABILITY: on FAT/exFAT, many network mounts
    and some overlays it raises ``OSError`` with ``EPERM``/``EOPNOTSUPP``/``ENOSYS`` instead.
    ``_publish_report`` runs AFTER ``promoted = True``, so such a failure there unwinds a
    SUCCESSFUL promotion -- a safety mechanism that assumes a capability becoming a new failure
    mode where the capability is absent, firing after the point of no return.

    So the capability is probed here, before the irreversible rename: create a private scratch
    file in the report's own parent and try to link it. A refusal before promotion is
    recoverable (the prior bundle is untouched); the same refusal after it is not. The probe
    runs in the exact directory the publish targets because hard-link support is per-filesystem,
    not per-host, and --out may sit on a different mount than anything else.
    """
    if not _pinned._dir_fd_supported():
        return
    parent = report_path.parent
    probe_src = parent / f".{_RUN_ID}.linkprobe.src"
    probe_dst = parent / f".{_RUN_ID}.linkprobe.dst"
    try:
        parent_fd = _pinned._open_dir_nofollow_pinned(parent)
    except OSError:
        # The parent cannot be pinned here; ``_publish_report`` will refuse cleanly on the same
        # open before promotion is involved, so leave that path to report it.
        return
    try:
        try:
            fd = os.open(
                probe_src.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW_READ_FLAGS,
                0o600,
                dir_fd=parent_fd,
            )
        except OSError:
            # Could not even create the scratch file (name taken, permissions). Not a hard-link
            # verdict -- let the publish path handle whatever is really wrong.
            return
        os.close(fd)
        try:
            os.link(probe_src.name, probe_dst.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno in _HARD_LINK_UNSUPPORTED_ERRNOS:
                raise ExportRefused(
                    f"the directory holding {report_path} does not support hard links "
                    f"({exc}). This build publishes its report by an exclusive hard link so a "
                    f"concurrent writer is refused rather than overwritten, and it will not "
                    f"promote a bundle it cannot then publish a report for. Point --out at a "
                    f"filesystem that supports hard links (a local ext4/xfs/apfs directory), "
                    f"not FAT/exFAT or this network mount."
                ) from exc
            # Any other link failure (a race on the probe name, ENOSPC) is not a capability
            # verdict; let the real publish surface it.
            return
        finally:
            try:
                os.unlink(probe_dst.name, dir_fd=parent_fd)
            except OSError:
                pass
    finally:
        try:
            os.unlink(probe_src.name, dir_fd=parent_fd)
        except OSError:
            pass
        os.close(parent_fd)


def _publish_report(report_tmp: Path, report_path: Path, report_before: "bytes | None") -> None:
    """Publish ``report_tmp`` at ``report_path`` with NO-REPLACE semantics, bound to one fd.

    ``os.replace(report_tmp, report_path)`` re-resolves ``report_path`` by NAME and OVERWRITES
    whatever is there, so a concurrent process that drops a foreign file at that path between
    the caller's checks and the install would have it clobbered -- "I chose this path" is not
    "I own what is at it now". This opens the parent once with ``O_NOFOLLOW | O_DIRECTORY``,
    re-checks the leaf by ``lstat`` against that descriptor, and then installs by EXCLUSIVE
    HARD LINK (``os.link``, which fails ``FileExistsError``) rather than a replace: a file that
    arrives in the window is ANSWERED by the link failing, not assumed away, and the collision
    is REFUSED. When the path already holds this build's own verified prior report, that report
    is moved aside first and restored (or preserved beside a racer's file) so no refusal path
    is ever destructive.

    Shape is not the whole of ownership. A value read back has four independent properties, and
    each can have changed since we last saw it: whether it EXISTS, whether it is the SAME OBJECT,
    whether its CONTENT is unchanged, and whether it is READABLE. The shape ``lstat`` covers the
    first two; a concurrent process that edits the report IN PLACE leaves the same object, still
    readable, with different bytes -- missing none of the first two, so a shape check alone says
    fine while a plain overwrite would destroy that edit. The build owns the report exclusively
    for the duration of one build (it only ever writes it through ``report_tmp`` + this publish,
    never in place), so the bytes at ``report_path`` must still equal what the caller read before
    the build (``report_before``), or the file must be absent. Anything else is a foreign edit,
    and the only definitely-wrong answer is to overwrite it -- a report is not mergeable, so drift
    is REFUSED. The content is read through the SAME descriptor the publish targets, so the bytes
    compared are the bytes that would be superseded.

    Consults ``_dir_fd_supported`` for the same reason every ``O_DIRECTORY`` user does: on a
    platform without descriptor-relative opens there is no atomic form, and the whole builder
    already refuses on such a platform before reaching here -- but the guard is stated locally
    so the rule that every ``O_DIRECTORY`` use is gated holds by reading, not by trust.
    """
    if not _pinned._dir_fd_supported():
        # Unreachable in practice (the builder refuses at its entry on such a platform), but a
        # by-name publish here would be the very window this helper closes, so refuse rather
        # than silently take it.
        raise ExportRefused(
            "cannot publish the report atomically without descriptor-relative opens on this "
            "platform; the builder is POSIX-only until that primitive exists."
        )
    try:
        parent_fd = _pinned._open_dir_nofollow_pinned(report_path.parent)
    except OSError as exc:
        # Pin every component of the report's parent, not just the leaf: opening the parent by
        # bare path string re-resolved it and followed a grandparent/intermediate swapped into
        # the window, after which the lstat, the content re-read, and the publish below all
        # run relative to a descriptor pointing outside --out. A component swapped after
        # resolution fails its own no-follow open and arrives here as a refusal.
        raise ExportRefused(
            f"cannot publish the report at {report_path}: a component of its directory is "
            f"not there, is not a directory this build can open, or changed to a link "
            f"({exc}). The path is derived from --out; point --out elsewhere."
        ) from exc
    try:
        try:
            st = os.lstat(report_path.name, dir_fd=parent_fd)
        except FileNotFoundError:
            st = None
        if st is not None and not stat.S_ISREG(st.st_mode):
            raise ExportRefused(
                f"{report_path} is not a plain file at publish time (it was replaced by "
                f"another object during the build). Refusing to overwrite it; point --out "
                f"elsewhere."
            )
        if st is not None:
            # Same object, still readable -- but is it the same CONTENT the caller read before
            # the build? Read it back through the SAME descriptor the publish will target
            # (no-follow, so a leaf swapped to a link is refused by the open, not chased), and
            # refuse if the bytes drifted: that is a concurrent in-place editor whose write the
            # publish would otherwise supersede without a trace.
            leaf_fd = os.open(
                report_path.name, os.O_RDONLY | _NOFOLLOW_READ_FLAGS, dir_fd=parent_fd
            )
            try:
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(leaf_fd, 65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                current = b"".join(chunks)
            finally:
                os.close(leaf_fd)
            if current != report_before:
                raise ExportRefused(
                    f"{report_path} was edited by another process while this build ran "
                    f"(its bytes changed since the build started). The report is written "
                    f"only through an atomic replace, so an in-place change is a foreign "
                    f"edit; refusing to overwrite it rather than destroy that write. "
                    f"Re-run the build once nothing else is writing there."
                )
        # Install with NO-REPLACE semantics. ``os.replace`` re-resolves the name and
        # OVERWRITES whatever is there, so a file a concurrent process drops at the report
        # path in the window between the checks above and here is destroyed silently -- "I
        # checked it a moment ago" is not "nothing got here since". A hard link ANSWERS the
        # question instead of assuming it: it fails ``FileExistsError`` rather than clobbering,
        # and a collision is REFUSED. Every refusal below leaves both the destination and the
        # staged ``report_tmp`` recoverable, so a raise here is never destructive.
        tmp_name = report_tmp.name
        leaf_name = report_path.name
        if st is None:
            # Nothing was here at the check above; publish by exclusive hard link. A file
            # created in the window lands as ``FileExistsError`` -> refuse, clobbering nothing.
            try:
                os.link(tmp_name, leaf_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            except FileExistsError:
                raise ExportRefused(
                    f"{report_path} was created by another process while this build ran, "
                    f"after the checks above found nothing there. Refusing to overwrite it. "
                    f"The staged report is kept. Re-run once nothing else is writing there."
                ) from None
        else:
            # The path held this build's own prior report, verified byte-identical to
            # ``report_before`` above. Move that verified report ASIDE within the directory,
            # then publish the new one by exclusive hard link. If a concurrent writer slips a
            # file in during the swap the link lands as ``FileExistsError``: the prior report
            # is preserved at the aside name and BOTH are left in place -- restoring the aside
            # over the name would destroy that concurrent write, so nothing is clobbered
            # either way.
            aside_name = leaf_name + f".{_RUN_ID}.prev"
            # Claim the aside name with an EXCLUSIVE link, not ``os.rename``: a rename REPLACES
            # whatever is already at ``aside_name``, so a foreign file a concurrent process
            # left at this run-id scratch name would be overwritten. ``os.link`` fails
            # ``FileExistsError`` on an occupant, so the scratch name is a checked claim -- if
            # something else holds it, refuse and name it rather than overwrite. Once the link
            # lands, both names point at the prior report's inode; the original name is then
            # unlinked so the leaf is free for the publish. A failure between the link and the
            # unlink leaves both names (two links to one inode), which the recovery below and
            # the operator can both resolve -- nothing is destroyed.
            try:
                os.link(leaf_name, aside_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            except FileExistsError:
                raise ExportRefused(
                    f"the scratch name {report_path}.{_RUN_ID}.prev is already held by "
                    f"another process; refusing to overwrite it. This build's report is not "
                    f"published and the existing report is untouched. Re-run once nothing "
                    f"else is writing there."
                ) from None
            os.unlink(leaf_name, dir_fd=parent_fd)
            try:
                os.link(tmp_name, leaf_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            except FileExistsError:
                raise ExportRefused(
                    f"{report_path} was replaced by another process while this build "
                    f"published its report. Refusing to overwrite it; this build's previous "
                    f"report is preserved at {leaf_name}.{_RUN_ID}.prev and the staged report "
                    f"is kept. Re-run once nothing else is writing there."
                ) from None
            except BaseException:
                # A different failure. The link did NOT publish, but that does not prove the
                # name is free: a concurrent writer may have created a file at ``leaf_name`` in
                # the window between the aside-move and here. Restore by EXCLUSIVE LINK
                # (``os.link``, which fails ``FileExistsError`` on an occupant), NOT
                # ``os.rename`` -- a rename replaces atomically and would destroy that
                # concurrent write. If the name is now occupied, PRESERVE the aside at its
                # ``.prev`` name and leave the occupant in place: residue an operator can
                # recover is the safe failure, overwriting an unknown occupant is the guess
                # (the transaction's contract -- when it cannot complete it leaves things
                # behind rather than overwriting or deleting anything it did not create).
                try:
                    os.link(aside_name, leaf_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                except FileExistsError:
                    # The destination reappeared. Do not clobber it; the prior report stays at
                    # the aside name for the operator to recover, and the original exception
                    # propagates unmasked.
                    pass
                except OSError:
                    # Restore itself failed for another reason: leave the aside in place rather
                    # than mask the original failure. Best effort.
                    pass
                else:
                    # The restore landed by link; drop the now-redundant aside copy.
                    try:
                        os.unlink(aside_name, dir_fd=parent_fd)
                    except OSError:
                        pass
                raise
            # Published: drop the aside copy of our own now-superseded prior report.
            try:
                os.unlink(aside_name, dir_fd=parent_fd)
            except OSError:
                pass
        # The publish left ``report_tmp`` as a second link to the published inode; drop it so
        # the run-id temp does not linger. Its absence (a reverted ``os.replace`` consumes it)
        # is not an error here.
        try:
            os.unlink(tmp_name, dir_fd=parent_fd)
        except OSError:
            pass
    finally:
        os.close(parent_fd)
