"""Which ``.app_secret`` leaves a cron child's sandbox hides, and the per-bundle
hard-link scan that refuses a cron whose own bundle aliases another app's secret.

Split out of :mod:`kiro_crew.cron_script` (which keeps the two runners that call
:func:`cron_apps_mask`) to stay under the per-file size rule. No behaviour change:
the symbols are the same objects, moved verbatim, and ``cron_script`` re-exports the
ones its public surface and the tests reference.
"""

from __future__ import annotations

import logging
import os
import stat
import tempfile
from dataclasses import dataclass

_APP_BUNDLE_SECRET_LEAVES: tuple[str, ...] = (".app_secret",)


@dataclass(frozen=True)
class CronAppsMask:
    """What a cron child is told about every app's ``.app_secret``.

    LEAF-ONLY mask. ``hidden`` is every installed app's ``.app_secret`` bearer
    credential (the cron's OWN app included), by absolute path in every spelling the
    crew home has -- NOT the whole apps tree. An app's code and its ``data/`` stay
    readable and writable exactly as on main; only the credential leaves are hidden.
    ``required`` is that same set listed as a FAIL-CLOSED mask: present at planning, so
    the launcher refuses rather than skips one gone by mask time (a secret moved aside
    between planning and the bind cannot come back unmasked). A leaf that may legitimately
    be ABSENT at spawn -- a ``.<name>-secret-tmp`` staging sibling an ``update_app`` leaves
    mid-swap, and the live name it will be restored to -- is in ``hidden`` AND ``guard``
    but NOT ``required``: requiring an absent path would refuse the run (and, for a stale
    staging file an interrupted update left behind, every cron on the gateway). ``guard``
    carries those mask-only leaves to the macOS Seatbelt rename/ancestor seal, so a child
    cannot rename the apps directory to read a staged credential under a path no deny names
    -- the namespace backend's bind masks already follow the dentry, so ``guard`` is
    Seatbelt-only.

    ``refusal`` is set when the cron's OWN bundle holds a hard link to any app's
    ``.app_secret`` inode: that link would expose the credential by a name the leaf
    mask does not cover, so the owning app's spawn is refused outright with a message
    naming the app and the linked path. A link inside some OTHER app's bundle costs
    that app nothing here, so one app's stray link refuses at most that one app, never
    every app. ``refusal`` is also set when the secret set could not be fully built,
    since the leaf mask cannot then be applied completely (fail closed).
    """

    hidden: tuple[str, ...] = ()
    required: tuple[str, ...] = ()
    guard: tuple[str, ...] = ()
    refusal: str = ""


def _app_name_under(path: str, real_apps_root: str) -> str:
    """The app whose bundle holds *path*, or ``""``.

    Judged on the RESOLVED path, so a link pointing into a bundle names that bundle and
    a link pointing out of one names nothing. A name that starts with ``.`` (the
    installer's staging siblings) or is not a plain directory is not a bundle.
    """
    try:
        real = os.path.realpath(path)
        rel = os.path.relpath(real, real_apps_root)
    except (OSError, ValueError):
        return ""
    if rel == os.curdir or rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return ""
    name = rel.split(os.sep, 1)[0]
    return name if _is_bundle_name(name, real_apps_root) else ""


def _is_bundle_token(name: str) -> bool:
    """A syntactically valid app-bundle name, without requiring the directory to exist.

    The name-level checks that gate every bundle name: non-empty, not a dotfile, no path
    separators or parent refs. Used where a live directory may legitimately be absent --
    an app whose bundle is mid-``update_app`` (its directory momentarily replaced) is
    still that app, so a decision keyed on its name must not depend on the directory.
    """
    return not (not name or name.startswith(".") or "/" in name or "\\" in name or ".." in name)


def _is_bundle_name(name: str, real_apps_root: str) -> bool:
    if not _is_bundle_token(name):
        return False
    candidate = os.path.join(real_apps_root, name)
    return os.path.isdir(candidate) and not os.path.islink(candidate)


def _path_within(child: str, parent: str) -> bool:
    """True when realpath(*child*) is *parent* or lies beneath it."""
    try:
        child_real = os.path.realpath(child)
        parent_real = os.path.realpath(parent)
    except OSError:
        # Cannot resolve one of them: treat as "possibly within" so the caller
        # takes the fail-safe branch rather than assuming it is outside the tree.
        return True
    if child_real == parent_real:
        return True
    return child_real.startswith(parent_real + os.sep)


def _safe_cron_cwd(real_apps_root: str) -> str | None:
    """A working directory for a cron child that cannot resolve into the apps tree.

    A cron child inherits the gateway process's working directory unless one is
    pinned. The apps-tree mask hides every app's ``.app_secret`` by ABSOLUTE path,
    but a child whose inherited cwd is itself inside the apps tree can read a sibling
    app's secret through a RELATIVE path (``cat other-app/.app_secret``), which never
    goes through the absolute mask. So the child must never run with a cwd under the
    apps tree.

    When the gateway's cwd is already outside the apps tree, pin it explicitly (an
    explicit cwd rather than an inherited one). When it is inside the tree, pin a
    neutral directory outside it instead -- the system temp root, which always exists
    and holds no app -- so a relative read reaches nothing. Returns ``None`` only when
    no safe directory can be established, and the caller then REFUSES the spawn (a
    child with an apps-tree cwd and no safe replacement must not launch).
    """
    try:
        cwd = os.getcwd()
    except OSError:
        # The gateway's own cwd is unreadable (e.g. it was removed). We cannot pin the
        # inherited one, so fall through to a neutral directory below.
        cwd = ""
    if cwd and not _path_within(cwd, real_apps_root):
        return cwd
    # The inherited cwd is inside the apps tree (or unknowable): pin a neutral dir.
    neutral = tempfile.gettempdir()
    try:
        if os.path.isdir(neutral) and not _path_within(neutral, real_apps_root):
            return neutral
    except OSError:
        pass
    return None


def _all_app_secret_ids(real_apps_root: str) -> tuple[set[tuple[int, int]], bool]:
    """``((st_dev, st_ino) set, complete)`` for every app's ``.app_secret`` (and staging copy).

    The set a per-bundle scan is checked against: a bundle holding a hard link to ANY of
    these inodes would expose that credential through a window, so the owning app's cron
    is refused. Reads the single-linked common case too, because the scan compares
    inodes, not link counts.

    ``complete`` is ``False`` when the set could not be fully built -- the apps root
    could not be scanned, or a candidate secret could not be ``lstat``-ed for a reason
    other than genuine absence (an unreadable app directory, EACCES). An incomplete set
    would make the hard-link scan FAIL OPEN: an inode missing from the set is never
    matched, so a link to it passes the scan and the window opens over a live credential.
    The caller treats an incomplete set as unsafe and withholds the window.
    """
    ids: set[tuple[int, int]] = set()
    complete = True
    candidates: list[str] = []
    try:
        with os.scandir(real_apps_root) as entries:
            for entry in entries:
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    complete = False
                    continue
                if is_dir:
                    candidates.extend(
                        os.path.join(entry.path, leaf) for leaf in _APP_BUNDLE_SECRET_LEAVES
                    )
                    continue
                try:
                    is_symlink = entry.is_symlink()
                except OSError:
                    complete = False
                    continue
                if is_symlink:
                    # An app installed as a SYMLINK to a directory is not itself a plain
                    # bundle directory, but its ``.app_secret`` bytes are still real
                    # and reachable, so a hard-link alias to them elsewhere must be
                    # caught. Resolve the link to a directory and enumerate its secret
                    # leaves.
                    try:
                        resolved_dir = entry.is_dir(follow_symlinks=True)
                    except OSError:
                        complete = False
                        continue
                    if resolved_dir:
                        candidates.extend(
                            os.path.join(entry.path, leaf) for leaf in _APP_BUNDLE_SECRET_LEAVES
                        )
                        continue
                    # A top-level symlink that does NOT resolve to a directory. One shape
                    # is legitimate: ``update_app`` moves ``.app_secret`` aside with
                    # ``shutil.move`` (apps/manager.py), so when the live secret was itself
                    # a symlink the staging ``.<name>-secret-tmp`` entry is a symlink to a
                    # regular file -- its referent is a live credential inode that a hard
                    # link elsewhere could alias, so it MUST become a candidate (the lstat
                    # loop follows the referent). Any OTHER non-dir symlink -- a dangling
                    # link, or one whose referent is not a directory -- carries no bundle
                    # and no secret inode this scan must pin, so it is GENUINE ABSENCE and
                    # is skipped, exactly as ``_all_app_secret_paths`` skips it. It is NOT
                    # ``complete = False``: a single stray top-level link (a symlinked
                    # bundle whose target was deleted, a layout the planner supports
                    # elsewhere) must not refuse every cron on the gateway -- the two scans
                    # must agree, or an inode-scan refusal the path-scan never raises breaks
                    # the "one app's stray entry refuses at most that one app" promise. A
                    # real ``OSError`` while inspecting still fails closed (handled above).
                    if entry.name.startswith(".") and entry.name.endswith("-secret-tmp"):
                        candidates.append(entry.path)
                    continue
                if entry.name.startswith(".") and entry.name.endswith("-secret-tmp"):
                    candidates.append(entry.path)
    except OSError:
        # The whole set is unknown: fail closed.
        return ids, False
    for path in candidates:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            # A bundle's live ``.app_secret`` can be absent for two reasons, and they
            # are not the same for safety. GENUINE absence (the app ships no secret)
            # is not a gap in the set. But an in-flight ``update_app`` moves the live
            # secret to a ``.<name>-secret-tmp`` sibling (apps/manager.py) BEFORE it is
            # moved back, so a ``.app_secret`` that vanished between the ``scandir``
            # snapshot and this ``lstat`` may be an update staging the inode elsewhere
            # -- and a hard link to that still-live inode in another app's tree would
            # ride into a window. Treating that as genuine absence drops the inode
            # while the set still reports complete -> the per-bundle scan fails OPEN.
            # So for a ``.app_secret`` candidate, recover the inode from the staging
            # sibling if an update is mid-flight; if the staging path cannot be pinned,
            # mark the set incomplete rather than silently dropping the credential.
            head, leaf = os.path.split(path)
            if leaf == _APP_BUNDLE_SECRET_LEAVES[0]:
                bundle_name = os.path.basename(head)
                staging = os.path.join(real_apps_root, f".{bundle_name}-secret-tmp")
                try:
                    staging_info = os.lstat(staging)
                except FileNotFoundError:
                    # The staging sibling is gone too. That is NOT yet proof of genuine
                    # absence: ``update_app`` moves the secret live -> staging -> live, so
                    # BOTH the live ``.app_secret`` lstat above and this staging lstat can
                    # miss if the secret moved BACK to its live name in the window between
                    # the two reads. The inode is then live under ``.app_secret`` again and
                    # a hard link to it in another app's tree would still ride into a
                    # window. Recheck the live name once and recover its inode -- following
                    # a symlink referent exactly as the regular live-candidate path below
                    # -- so a secret restored as a SYMLINK (``update_app`` moves a symlinked
                    # secret back to its live name) is still pinned rather than silently
                    # dropped by a plain-file-only check while the set reports complete. If
                    # the live name is STILL absent, no secret exists under either name at
                    # this instant: that is genuine absence (a bundle that ships no secret
                    # -- a builtin owner installs none), which is not a gap in the set, so
                    # leave it masked and keep the set complete rather than refusing every
                    # secret-less app. Any OTHER inspection failure (an unreadable inode, or
                    # a referent we cannot resolve) is NOT genuine absence, so fail CLOSED
                    # by marking the set incomplete rather than dropping a possible
                    # credential.
                    try:
                        live_info = os.lstat(path)
                    except FileNotFoundError:
                        continue
                    except OSError:
                        complete = False
                        continue
                    if stat.S_ISREG(live_info.st_mode):
                        ids.add((live_info.st_dev, live_info.st_ino))
                    elif stat.S_ISLNK(live_info.st_mode):
                        try:
                            live_target = os.stat(path)  # follows the symlink referent
                        except OSError:
                            complete = False
                            continue
                        if stat.S_ISREG(live_target.st_mode):
                            ids.add((live_target.st_dev, live_target.st_ino))
                        else:
                            complete = False
                    else:
                        complete = False
                    continue
                except OSError:
                    # The staging inode exists but its identity is unreadable: the set
                    # is incomplete and the scan would fail open against it.
                    complete = False
                    continue
                if stat.S_ISREG(staging_info.st_mode):
                    ids.add((staging_info.st_dev, staging_info.st_ino))
                elif stat.S_ISLNK(staging_info.st_mode):
                    try:
                        staging_target = os.stat(staging)
                    except OSError:
                        complete = False
                        continue
                    if stat.S_ISREG(staging_target.st_mode):
                        ids.add((staging_target.st_dev, staging_target.st_ino))
                    else:
                        complete = False
                else:
                    complete = False
                continue
            # A top-level ``.<name>-secret-tmp`` staging candidate that vanished is an
            # update that moved its secret BACK to the live name. It is NOT safe to treat
            # that as genuine absence: ``update_app`` RETIRES the bundle directory while it
            # swaps the tree (``os.replace(dest, retired)`` in apps/manager.py), so the
            # bundle dir can be missing from the ``scandir`` snapshot entirely -- meaning
            # the bundle's live ``.app_secret`` was NEVER added as a candidate. If the
            # staging file then moves back to the live name before this ``lstat``, dropping
            # it here skips the inode while the set still reports complete -> the per-bundle
            # scan fails OPEN against a hard link to that credential. So recover the inode
            # from the corresponding live ``.app_secret`` (following a symlink referent, the
            # same as the live-candidate path below); if that inspection fails for a reason
            # other than genuine absence, mark the set incomplete rather than silently
            # skipping it.
            staging_leaf = os.path.basename(path)
            if staging_leaf.startswith(".") and staging_leaf.endswith("-secret-tmp"):
                bundle_name = staging_leaf[1 : -len("-secret-tmp")]
                live = os.path.join(real_apps_root, bundle_name, _APP_BUNDLE_SECRET_LEAVES[0])
                try:
                    live_info = os.lstat(live)
                except FileNotFoundError:
                    # Neither the staging copy nor the live name resolves now: no secret
                    # exists for this bundle at this instant (genuine absence), not a
                    # dropped inode.
                    continue
                except OSError:
                    complete = False
                    continue
                if stat.S_ISREG(live_info.st_mode):
                    ids.add((live_info.st_dev, live_info.st_ino))
                elif stat.S_ISLNK(live_info.st_mode):
                    try:
                        live_target = os.stat(live)  # follows the symlink to the referent
                    except OSError:
                        complete = False
                        continue
                    if stat.S_ISREG(live_target.st_mode):
                        ids.add((live_target.st_dev, live_target.st_ino))
                    else:
                        complete = False
                else:
                    complete = False
            continue
        except OSError:
            # EACCES or similar: the inode exists but we could not read its identity, so
            # the set is incomplete and the scan would fail open against it.
            complete = False
            continue
        if stat.S_ISREG(info.st_mode):
            ids.add((info.st_dev, info.st_ino))
        elif stat.S_ISLNK(info.st_mode):
            # The secret is a symlink. ``validate_app_secret`` reads the credential
            # through it, so the bytes live at the symlink's REFERENT. A hard link to
            # that referent elsewhere is still an alias of the credential, so the
            # referent's inode -- not the symlink's -- is what the scan must match.
            try:
                target = os.stat(path)  # follows the symlink
            except OSError:
                # Dangling or unreadable referent: we cannot pin the inode to protect,
                # so the set is incomplete and the caller must fail closed.
                complete = False
                continue
            if stat.S_ISREG(target.st_mode):
                ids.add((target.st_dev, target.st_ino))
            else:
                # The referent is not a regular file (a dir, a device, a symlink loop):
                # not a credential inode we can alias-match, so record the set as
                # incomplete rather than silently ignoring it.
                complete = False
    return ids, complete


def _all_app_secret_paths(real_apps_root: str) -> tuple[list[str], list[str], bool]:
    """``(required, mask_only, complete)`` -- every installed app's ``.app_secret`` leaf.

    ``required`` is every live, present ``.app_secret`` leaf (``<app>/.app_secret``): a
    fail-closed mask the launcher asserts is present. ``mask_only`` is every leaf that
    must be MASKED but may legitimately be ABSENT at spawn -- a ``.<name>-secret-tmp``
    staging sibling an ``update_app`` leaves while it swaps a tree (that staging file IS a
    live credential until the update finishes), and the matching live leaf name it will
    be restored to. A ``mask_only`` entry is masked by NAME on both backends (a path rule
    that holds whether or not the name exists) but is NEVER required, so a stale staging
    file an interrupted update left behind masks the credential WITHOUT forcing every cron
    on the gateway to refuse. The path is masked by NAME, so a plain-file secret and a
    symlinked one are both covered.
    every bundle directory under *real_apps_root*, plus any ``.<name>-secret-tmp`` staging
    sibling an ``update_app`` leaves while it swaps a tree (that staging file IS a live
    credential until the update finishes). The path is masked by NAME, so a secret that
    is a plain file and one that is a symlink are both covered -- masking the leaf name
    hides whatever the child would open at it.

    ``complete`` is ``False`` only when the set genuinely could not be built: the apps
    root could not be scanned, a candidate directory could not be classified, or a
    candidate leaf could not be ``lstat``-ed for a reason OTHER than genuine absence (an
    unsearchable bundle directory, EACCES). A bundle whose live ``.app_secret`` is absent
    while an ``update_app`` is mid-flight (a ``.<name>-secret-tmp`` staging sibling exists)
    does NOT flip ``complete``: the staging name and the live name it will be restored to
    are both added to ``mask_only`` instead, so the credential is masked wherever the
    update lands it without refusing unrelated crons. Presence is decided with
    ``os.lstat`` rather than ``os.path.lexists`` because ``lexists`` returns ``False`` on
    ANY error -- including ``EACCES`` -- so a secret present but momentarily unsearchable
    (one cron child can flip a bundle directory to mode 000 between this scan and the
    spawn) would be read as absent while ``complete`` stayed ``True``, and the spawn would
    run with that secret unmasked. A bundle that simply ships no secret, with no staging
    sibling, is genuine absence (``FileNotFoundError``): it contributes no path.
    """
    paths: list[str] = []
    mask_only: list[str] = []
    complete = True
    try:
        with os.scandir(real_apps_root) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False) or entry.is_dir(follow_symlinks=True):
                        # A bundle directory (or a symlink to one): its live secret, if
                        # present, is masked by name.
                        for leaf in _APP_BUNDLE_SECRET_LEAVES:
                            secret = os.path.join(entry.path, leaf)
                            try:
                                os.lstat(secret)
                            except FileNotFoundError:
                                secret_present = False
                            except OSError:
                                # Present but not ``lstat``-able (EACCES on an unsearchable
                                # bundle dir, etc.): do NOT treat it as absent, which would
                                # leave a live secret unmasked. Fail closed -- the caller
                                # refuses the spawn.
                                complete = False
                                continue
                            else:
                                secret_present = True
                            if secret_present:
                                paths.append(secret)
                            elif leaf == _APP_BUNDLE_SECRET_LEAVES[0]:
                                # The live secret is genuinely absent (FileNotFoundError).
                                # Distinguish a secretless app from an in-flight
                                # ``update_app`` that moved it to a ``.<name>-secret-tmp``
                                # sibling AFTER ``scandir`` buffered these entries. Probe the
                                # staging path DIRECTLY (a fresh filesystem check): if it
                                # exists, a credential is live under the staging name, so
                                # mask BOTH the staging path and the live leaf as MASK-ONLY
                                # (present-if-and-when, never required) -- NOT ``complete =
                                # False``. A stale ``.<name>-secret-tmp`` left behind by an
                                # update the gateway died partway through would otherwise
                                # make EVERY cron on the gateway refuse until that one app is
                                # updated again; masking the leaf closes the window without
                                # that gateway-wide denial. An ``lstat`` error OTHER than
                                # genuine absence still fails closed.
                                staging = os.path.join(real_apps_root, f".{entry.name}-secret-tmp")
                                try:
                                    os.lstat(staging)
                                except FileNotFoundError:
                                    # Both the live leaf and the staging sibling missed.
                                    # That is NOT yet proof of a secretless app: an in-flight
                                    # ``update_app`` moves the secret live -> staging -> live,
                                    # so the secret may have been RESTORED to its live name in
                                    # the window between the live-path probe above and this
                                    # staging probe. Re-check the live name once: if it is
                                    # back, mask it (a credential is live under it again); if
                                    # it is still absent, this is genuine absence; an error
                                    # other than absence fails closed.
                                    try:
                                        os.lstat(secret)
                                    except FileNotFoundError:
                                        pass
                                    except OSError:
                                        complete = False
                                    else:
                                        paths.append(secret)
                                except OSError:
                                    complete = False
                                else:
                                    # Staging present, live absent: mask both names so the
                                    # credential is covered wherever the update lands it,
                                    # without requiring either to exist at spawn.
                                    mask_only.append(staging)
                                    mask_only.append(secret)
                        continue
                except OSError:
                    complete = False
                    continue
                # A top-level ``.<name>-secret-tmp`` staging file is a live credential
                # mid-update and must be masked too. The update moves the secret
                # staging -> live, so the live leaf may be restored under its bundle at any
                # moment; mask BOTH names as MASK-ONLY so the credential is covered wherever
                # it lands, without requiring an absent path to exist at spawn (which would
                # make the caller refuse every cron on a stale staging file).
                name = entry.name
                if name.startswith(".") and name.endswith("-secret-tmp"):
                    mask_only.append(entry.path)
                    bundle_name = name[1 : -len("-secret-tmp")]
                    if bundle_name:
                        mask_only.append(
                            os.path.join(real_apps_root, bundle_name, _APP_BUNDLE_SECRET_LEAVES[0])
                        )
    except OSError:
        return paths, mask_only, False
    return paths, mask_only, complete


def _bundle_links_a_secret(
    bundle: str, secret_ids: set[tuple[int, int]], own_secret: tuple[int, int] | None
) -> str:
    """The first path inside *bundle* that is a hard link to another app's secret, or ``""``.

    Walks the bundle tree once. A regular file whose inode is in *secret_ids* and is not
    the bundle's OWN single ``.app_secret`` (``own_secret``) is a reachable alias to a
    credential -- the per-window scan the re-land plan asks for, run per bundle so one
    app's stray link refuses only that app. ``own_secret`` is excluded because the
    bundle's own secret is masked by name inside the window regardless; a SECOND link to
    it elsewhere in the same bundle, however, is still reported (it is not at the masked
    name). Symlinks are not followed: a symlink out of the tree names bytes the window
    does not expose, and ``realpath`` on every entry would cost a walk of linked trees.

    FAIL-CLOSED on an unreadable subtree. ``os.walk`` silently skips a directory it
    cannot enter (a mode-000 ``data/private/``), so an unguarded walk would miss a link
    planted there and open the window anyway -- and the owned bundle's ``data/`` is a
    writable window the child can ``chmod`` back open. An ``onerror`` that records the
    failure makes any traversal error a scan the caller must treat as unsafe, reported
    the same as a found alias so the caller withholds the window rather than opening one
    it could not fully scan.
    """
    own_secret_path = os.path.join(bundle, _APP_BUNDLE_SECRET_LEAVES[0])
    scan_failed: list[str] = []

    def _onerror(exc: OSError) -> None:
        scan_failed.append(getattr(exc, "filename", None) or bundle)

    for dirpath, dirnames, filenames in os.walk(bundle, followlinks=False, onerror=_onerror):
        if scan_failed:
            return scan_failed[0]
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                info = os.lstat(full)
            except OSError:
                # A file we cannot even lstat is an unreadable entry inside the tree we
                # are about to window: treat it as unsafe rather than skip it.
                return full
            if not stat.S_ISREG(info.st_mode):
                continue
            ident = (info.st_dev, info.st_ino)
            if ident not in secret_ids:
                continue
            # The bundle's own secret at its own name is masked by name; skip exactly
            # that path, but report a SECOND link to it anywhere else in the bundle.
            if own_secret is not None and ident == own_secret and full == own_secret_path:
                continue
            return full
    if scan_failed:
        return scan_failed[0]
    return ""


def cron_apps_mask(
    *,
    script_file: str = "",
    owner_app: str = "",
) -> CronAppsMask:
    """The ``.app_secret`` leaf mask for one cron child.

    Masks EVERY installed app's ``.app_secret`` bearer credential (the cron's OWN app
    included) by absolute path, in every spelling the crew home has (a symlinked
    ``$HOME`` reaches one directory by two names, and a mask on one name is no mask on
    the other). It does NOT mask the apps tree: an app's code and its ``data/`` stay
    readable and writable from a cron child exactly as on main -- only the credential
    leaves are hidden. ``.app_secret`` is a bearer credential (``dashboard.token_auth``
    exchanges it for that app's scoped token), so no cron child can read any app's; an
    app cron reaches the gateway through ``ScriptContext`` (``ctx.notify``,
    ``ctx.call_tool``) instead of its on-disk secret.

    Each masked secret is also a ``required`` mask: present when this planned, so the
    launcher refuses rather than skips one gone by mask time -- a secret moved aside
    between planning and the bind cannot come back unmasked.

    FAIL CLOSED. ``script_file`` / ``owner_app`` name the cron's OWN bundle; it is
    scanned once for a hard link to any app's ``.app_secret`` inode
    (:func:`_bundle_links_a_secret`), because such a link exposes the credential by a
    name the leaf mask does not cover. A link in the OWN bundle refuses the whole spawn
    (``refusal`` set), naming the app and the path; a link in some other app's bundle
    costs that app nothing here, so one app's stray link refuses at most that one app. A
    secret the scan could not open or identify (an incomplete id set) also refuses,
    rather than launching with a secret left unmasked. A link at a path OUTSIDE the apps
    tree stays a known gap (tracked separately). Blocking (``stat``/``mkdir``/``walk``),
    so it runs in the cron worker thread, never on the event loop.
    """
    from kiro_crew.apps.manager import apps_dir
    from kiro_crew.sandbox import crew_home_visible_spellings

    root = apps_dir()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        # A root that cannot be created cannot be scanned for the secret leaves, so the
        # leaf mask cannot be built. Fail closed: refuse rather than launch with no mask.
        logging.getLogger(__name__).warning(
            "could not create %s to mask secrets for a cron child", root, exc_info=True
        )
        return CronAppsMask(
            refusal=(
                f"cron refused: {root} could not be created, so app secrets cannot be "
                f"masked for this cron child. Fix the directory's permissions and retry."
            ),
        )
    real_root = os.path.realpath(root)

    # The cron's OWN bundle (if any), for the item-3 hard-link scan only. No window is
    # opened for it: the apps tree is not masked, so the bundle is already readable.
    owned_name = ""
    if script_file:
        owned_name = _app_name_under(script_file, real_root)
    if not owned_name and owner_app and _is_bundle_token(owner_app):
        owned_name = owner_app

    # Every app's ``.app_secret`` inode (for the alias scan) and leaf path (to mask).
    secret_ids, secrets_complete = _all_app_secret_ids(real_root)
    secret_paths, mask_only_paths, paths_complete = _all_app_secret_paths(real_root)
    if not (secrets_complete and paths_complete):
        # The secret set could not be fully built (an unreadable app directory), so the
        # leaf mask would be incomplete and the alias scan would fail OPEN. Fail closed.
        return CronAppsMask(
            refusal=(
                "cron refused: an installed app's directory could not be read to build "
                "the app-secret mask, so a secret could be left readable. Retry once the "
                "apps tree is readable."
            ),
        )

    # Item 3: a hard link to any app's secret inside the cron's OWN bundle exposes the
    # credential by a name the leaf mask does not cover -- refuse that one app's spawn.
    if owned_name and _is_bundle_name(owned_name, real_root):
        bundle_real = os.path.join(real_root, owned_name)
        own_secret = _plain_file_identity(os.path.join(bundle_real, _APP_BUNDLE_SECRET_LEAVES[0]))
        linked = _bundle_links_a_secret(bundle_real, secret_ids, own_secret)
        if linked:
            return CronAppsMask(
                refusal=(
                    f"cron for app {owned_name!r} refused: its bundle holds a hard link "
                    f"to an app secret at {linked!r}; running it would expose that "
                    f"credential. Remove the extra link and retry."
                ),
            )

    # Mask every app's secret leaf by absolute path, in every crew-home spelling. A
    # PRESENT live leaf is also listed as ``required`` (a fail-closed mask the launcher
    # asserts is present); a MASK-ONLY leaf (a staging sibling, or the live name a stale
    # staging file will be restored to) is hidden but NOT required, because it may
    # legitimately be absent at spawn and requiring an absent path would make the launcher
    # refuse the run -- including crons unrelated to the app being updated.
    spellings = list(crew_home_visible_spellings(str(root)))

    def _expand(real_secret: str) -> list[str]:
        out: list[str] = []
        rel = os.path.relpath(real_secret, real_root)
        for spelling in spellings:
            out.append(os.path.join(spelling, rel))
        # A secret stored behind a SYMLINK is read through its referent. The mask above
        # hides the symlink NAME, and on the namespace backend the bind mask on that name
        # is enough; on macOS Seatbelt a deny names a path and the profile resolves the
        # symlink, so the referent -- which may sit OUTSIDE the apps tree -- must be masked
        # by its own absolute path too, or a cron child reads the bytes at the referent.
        # Add it directly (not relative to the apps root): the mask set accepts absolute
        # paths, and the launcher/profile denies read, write and link on it. A plain-file
        # secret has no referent to add (its realpath is itself, already covered).
        try:
            if os.path.islink(real_secret):
                referent = os.path.realpath(real_secret)
                if referent and referent != real_secret:
                    out.append(referent)
        except OSError:
            # Could not classify the leaf: the completeness check above already passed, so
            # this is a transient; the name mask still stands. Leave the referent off
            # rather than guess a path to deny.
            pass
        return out

    required_list: list[str] = []
    for real_secret in secret_paths:
        required_list.extend(_expand(real_secret))
    guard_list: list[str] = []
    for real_secret in mask_only_paths:
        guard_list.extend(_expand(real_secret))
    hidden = list(required_list) + guard_list
    required = tuple(dict.fromkeys(required_list))
    guard = tuple(dict.fromkeys(guard_list))
    hidden_deduped = tuple(dict.fromkeys(hidden))
    return CronAppsMask(hidden=hidden_deduped, required=required, guard=guard)


def _plain_file_identity(path: str) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` of *path* when it is a plain regular file (never a link)."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return (info.st_dev, info.st_ino) if stat.S_ISREG(info.st_mode) else None
