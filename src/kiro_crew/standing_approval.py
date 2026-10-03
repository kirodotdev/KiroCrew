"""The operator's STANDING declaration that every tool approval is skipped.

``SafetyOverride`` has two kinds of grant. An **ad-hoc** one is toggled mid-session
and expires on ``agent.yolo_duration``. A **declared** one is the operator's standing
instruction: it never expires, and it is re-established on every startup. This module
owns where that declaration is read from, and nothing else -- establishing the grant
stays with ``safety_override``.

Where the declaration lives, and why not ``config.json``
-------------------------------------------------------
``standing-approval/grant.json`` sits on the KEYSTONE floor
(``security._CREW_SECRET_LEAVES``) and is bind-masked out of every agent sandbox
(``sandbox._CREW_HIDDEN_LEAVES``). That is the same placement as
``computer_use.json``, ``aws_service_consent.json``, ``oauth_endpoints.json``,
``file_delivery_consent.json`` and ``ssh_auth_sock_consent.json``, and for the same
reason: this is an authorization, not a preference. It is the widest one the product
has -- every tool call in every future session, with no prompt and no expiry.

The declaration is deliberately NOT ``agent.dangerously_skip_permissions`` in
``config.json``. A read-only seal on that document closes a write to the sealed NAME,
and it is kept as defence in depth, but a seal cannot reach the inode behind the name:
the crew data-home root is writable in every sandbox, ``link(2)`` needs no write
permission on the file it copies a name for, and a bind mount seals a MOUNT rather
than an inode. So an in-sandbox process that owns that document can add a second name
for it in the writable root, write the standing posture through that name, and unlink
it -- and the next startup reads a poisoned document under a single link.

Two properties of this keystone answer that, and only the pair does:

* **Masked, not sealed.** A masked leaf cannot be opened in-sandbox at all, so it is
  not a ``link(2)`` source. A sealed-but-readable one still is.
* **A directory, not a file.** Linux refuses ``link(2)`` on a directory outright, so
  the alias shape has no source here even in principle, and a directory bind covers
  every child name rather than one pinned inode.

How the operator grants it
--------------------------
Like ``oauth_endpoints.json``, the operator writes the leaf out-of-band, from outside
the agent sandbox::

    mkdir -p "$KIROCREW_HOME/standing-approval"
    printf '{"dangerously_skip_permissions": true}\\n' \\
        > "$KIROCREW_HOME/standing-approval/grant.json"

There is deliberately no dashboard toggle (there never was one for this switch) and
no CLI verb: a surface that records this grant on request is a grant an automated
caller can take. This module is READ-ONLY on purpose.

Migrating an existing declaration
---------------------------------
An operator who set ``agent.dangerously_skip_permissions: true`` in ``config.json``
loses the standing grant until they write the keystone. That break is deliberate and
it is announced rather than silent: :func:`migration_notice` returns the words a
startup logs when the retired key is still set and the keystone is absent, and the
startup grants NOTHING in that state. Silently honouring the old location would keep
exactly the writable declaration this move exists to retire.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from typing import Any

from kiro_crew import sandbox
from kiro_crew.config.loader import (
    standing_approval_init_marker_path,
    standing_approval_path,
)

logger = logging.getLogger(__name__)

#: The field inside the keystone document. Spelled the same as the retired
#: ``config.json`` key on purpose: an operator moving the declaration copies one line
#: rather than learning a second name for the same switch.
GRANT_FIELD: str = "dangerously_skip_permissions"

#: Size bound for the grant document. It holds one boolean, so a few hundred bytes is
#: generous; anything larger is not a legitimate grant. The bound is what stops a huge
#: file at this name from being buffered into the gateway's memory on a synchronous
#: boot read.
_MAX_GRANT_BYTES: int = 4096


def _read_grant_bytes() -> bytes | None:
    """Read the grant document, refusing a link, a non-regular file or a second name.

    A plain ``read_text()`` follows a symlink, which would undo this leaf's whole
    point. The keystone directory is bind-masked and pre-created, so an in-sandbox
    process cannot place the link itself -- but an operator who aliases the document
    onto a sandbox-visible path (a dotfile manager is the ordinary way that happens)
    would hand the agent a writable name for the file that authorizes it. The reader
    refuses that shape rather than resolving it, which is the same disposition
    ``sandbox`` takes for a ceiling it cannot cover under a second name.

    Defences, in order, following :func:`session_pid_sig._read_regular_nofollow`:

    * ``O_NOFOLLOW | O_DIRECTORY`` on the PARENT directory, then an ``openat`` of the
      leaf relative to that pinned descriptor (``dir_fd``): ``O_NOFOLLOW`` on the leaf
      alone refuses a symlinked ``grant.json`` but NOT a symlinked ``standing-approval``
      directory, and the mask predicate matches by ``normpath`` without resolving links,
      so an operator-aliased directory (a dotfile manager is the ordinary way that
      happens) would still answer masked while handing the agent a writable parent for
      the file that authorizes it. Pinning the parent no-follow refuses the aliased
      directory at the open, and the dir_fd-relative open cannot traverse out of it.
    * ``O_NOFOLLOW`` (POSIX) on the leaf: the open itself refuses a symlink final
      component, race-free where an ``is_symlink()`` pre-check is not.
    * ``lstat`` pre-check plus a post-open identity check, for the one platform with
      no ``O_NOFOLLOW``: a link planted between the two opens its TARGET, whose
      ``(st_dev, st_ino)`` cannot match the vetted regular file.
    * ``S_ISREG``: rejects a FIFO or device at that name. ``O_NONBLOCK`` is in the
      open flags for that case specifically: a blocking ``O_RDONLY`` open of a FIFO
      with no writer never returns, so without it a FIFO planted at this name would
      hang the gateway's boot instead of being rejected by the check below. The flag
      is ignored for a regular file, which is every legitimate case.
    * ``st_nlink == 1``: a second hard link is a second writable name for this very
      inode, and the mask covers a path rather than an inode -- the exact shape this
      keystone exists to deny, so the reader must not accept a document carrying one.
    * A size bound checked against both ``fstat`` and the bytes actually read.

    **Why the deltas are not folded into that shared reader.**
    :func:`session_pid_sig._read_regular_nofollow` is read by ``session_pid_sig`` and
    ``session_token_sig``, and neither delta here is neutral for them. ``st_nlink == 1``
    is a REFUSAL, so folding it in refuses a legitimately hard-linked pid or token
    signature -- the ordinary output of a snapshot or dotfile tool, and harmless for those
    two because their documents are not authorizations: a second name for one grants
    nothing, while a second name for THIS one is the whole attack. Making the refusal
    conditional adds a parameter whose meaning is "be an authorization reader", which is
    this function under another name. ``O_NONBLOCK`` could fold on its own and would close
    a FIFO hang on that helper's own agent-writable mapping directory, but that changes
    two other modules' startup behaviour and belongs with whoever owns them rather than
    riding in on this leaf.

    Returns ``data`` on success -- the vetted document bytes -- and ``None`` on any
    refusal or I/O error, so every caller fails closed. The descriptor's release counts
    as part of the read: a failing ``close`` is caught here and resolves to no grant,
    because this reader runs on the gateway's startup thread outside any ``try``, so an
    error escaping it would abort boot instead of withholding a grant.
    """
    path = standing_approval_path()
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    pre: os.stat_result | None = None
    o_directory = getattr(os, "O_DIRECTORY", 0)
    parent = os.path.dirname(path)
    leaf = os.path.basename(path)
    parent_fd: int | None = None
    try:
        if nofollow and o_directory:
            # Pin the PARENT DIRECTORY no-follow, then open the leaf RELATIVE to that
            # pinned descriptor (openat via dir_fd). O_NOFOLLOW on the leaf alone
            # refuses a symlinked ``grant.json`` but not a symlinked ``standing-approval``
            # directory: an operator who aliases the DIRECTORY (a dotfile manager is the
            # ordinary way that happens) onto a sandbox-visible target hands the agent a
            # writable parent for the file that authorizes it, while the mask predicate --
            # which matches by ``normpath`` and never resolves links -- still answers
            # "masked". O_NOFOLLOW | O_DIRECTORY on the parent refuses the aliased
            # directory at the open, race-free, and the subsequent dir_fd-relative open
            # cannot traverse out of the pinned inode.
            parent_fd = os.open(parent, os.O_RDONLY | nofollow | o_directory | nonblock)
            fd = os.open(leaf, os.O_RDONLY | nofollow | nonblock, dir_fd=parent_fd)
        else:
            # No O_NOFOLLOW/O_DIRECTORY (Windows): the keystone grants nothing on any
            # delegated-sandbox platform (`is_declared` refuses there), so this is the
            # best-effort leaf guard -- lstat pre-check plus the post-open identity check
            # below -- rather than a parent pin the platform cannot promise.
            if not nofollow:
                pre = os.lstat(path)
                if stat.S_ISLNK(pre.st_mode):
                    logger.warning("standing auto-approve keystone is a symlink; refusing it")
                    return None
            fd = os.open(path, os.O_RDONLY | nofollow | nonblock)
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning(
            "standing auto-approve keystone could not be opened as a plain file; "
            "treating approvals as required"
        )
        return None
    finally:
        if parent_fd is not None:
            try:
                os.close(parent_fd)
            except OSError:
                pass
    closed_cleanly = True
    try:
        st = os.fstat(fd)
        if pre is not None and (st.st_dev, st.st_ino) != (pre.st_dev, pre.st_ino):
            return None
        if not stat.S_ISREG(st.st_mode):
            logger.warning("standing auto-approve keystone is not a regular file; refusing it")
            return None
        if st.st_nlink != 1:
            logger.warning(
                "standing auto-approve keystone has %d hard links, so the grant is "
                "reachable under another name; refusing it",
                st.st_nlink,
            )
            return None
        if st.st_size > _MAX_GRANT_BYTES:
            return None
        chunks: list[bytes] = []
        remaining = _MAX_GRANT_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        return None
    finally:
        try:
            os.close(fd)
        except OSError:
            closed_cleanly = False
    if not closed_cleanly:
        logger.warning(
            "standing auto-approve keystone could not be closed; treating approvals as required"
        )
        return None
    data = b"".join(chunks)
    if len(data) > _MAX_GRANT_BYTES:
        return None
    return data


def _read_all() -> dict[str, Any]:
    """The whole document, or ``{}`` when it is missing, refused or unreadable.

    Failing soft is the right READ behaviour: an authorization record that cannot be
    parsed is not an authorization, so the grant stays withheld and the session
    prompts. An absent document, a refused shape and an unparseable one resolve
    identically, which is also what makes the empty mask a sandboxed reader would see
    equal to no grant.
    """
    read = _read_grant_bytes()
    if read is None:
        return {}
    try:
        raw = json.loads(read.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.warning(
            "standing auto-approve keystone is unreadable; treating approvals as required"
        )
        return {}
    return raw if isinstance(raw, dict) else {}


def _marker_is_authentic() -> bool:
    """Whether a VALID gateway-written first-trusted-init marker is present.

    The marker is a trust root, so it is read with the same anti-forgery discipline
    :func:`_read_grant_bytes` applies to the grant, not with a bare ``exists()`` /
    ``lstat`` that an alias satisfies: the parent directory is pinned ``O_NOFOLLOW |
    O_DIRECTORY`` and the marker opened RELATIVE to that descriptor, and the opened
    file must be a regular file with exactly one hard link. A symlink, an aliased
    parent, a FIFO, or a second-named marker is therefore NOT authentic -- it is the
    shape a pre-upgrade agent plants to supply its own boundary -- and resolves the
    same as absent, so :func:`establish_trusted_init` re-draws the real one over it.

    Returns ``True`` only for a genuine gateway marker; ``False`` for absent, forged
    or unreadable (fail closed -- an unverifiable trust root is no trust root). On a
    platform without ``O_NOFOLLOW``/``O_DIRECTORY`` (Windows) the keystone grants
    nothing anyway (:func:`is_declared` refuses on a delegated-sandbox host), so the
    best-effort ``lstat`` symlink check is sufficient there.
    """
    path = str(standing_approval_init_marker_path())
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    o_directory = getattr(os, "O_DIRECTORY", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    parent = os.path.dirname(path)
    leaf = os.path.basename(path)
    parent_fd: int | None = None
    try:
        if nofollow and o_directory:
            # O_NONBLOCK on both opens: a pre-upgrade agent can plant the marker name as
            # a FIFO (marker.exists() is true for one), and a blocking O_RDONLY open of a
            # writer-less FIFO hangs the gateway boot path forever. The post-open
            # S_ISREG check below rejects the FIFO, but only once the open returns -- so
            # the flag is what makes that check reachable. The grant read path
            # (_read_grant) already opens non-blocking for the same reason.
            parent_fd = os.open(parent, os.O_RDONLY | nofollow | o_directory | nonblock)
            fd = os.open(leaf, os.O_RDONLY | nofollow | nonblock, dir_fd=parent_fd)
        else:
            pre = os.lstat(path)
            if stat.S_ISLNK(pre.st_mode):
                return False
            fd = os.open(path, os.O_RDONLY | nofollow | nonblock)
    except OSError:
        return False
    finally:
        if parent_fd is not None:
            try:
                os.close(parent_fd)
            except OSError:
                pass
    try:
        st = os.fstat(fd)
        return stat.S_ISREG(st.st_mode) and st.st_nlink == 1
    except OSError:
        return False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def establish_trusted_init(requested_mode: str) -> None:
    """Draw this gateway's FIRST trusted-init boundary, under the mask, once.

    The trust root the pre-upgrade plant residual lacked. On a release BEFORE the
    keystone directory was masked, the crew data-home root was agent-writable, so a
    hostile auto-approved agent could plant ``standing-approval/grant.json`` = ``True``
    and wait for an upgrade to read it back as the operator's standing authority.

    The marker filename is a published constant in this public repo, so a pre-upgrade
    agent can plant the marker TOO. That is why a pre-existing marker is **never**
    adopted as the boundary -- the earlier revision that trusted its mtime was
    forgeable by exactly that write (a backdated ``.trusted-init`` beside a newer
    ``grant.json`` certified the plant). The boundary is instead drawn here, by the
    GATEWAY, only when :func:`_keystone_is_masked` holds -- a condition the pre-upgrade
    (unmasked) release could not meet, and that an in-sandbox agent on THIS version
    cannot meet either, because the whole ``standing-approval`` directory is bind-masked
    (``sandbox._CREW_HIDDEN_LEAVES``) out of its reach.

    When no AUTHENTIC gateway marker exists yet (:func:`_marker_is_authentic` -- a
    planted symlink/alias/multi-link marker does not count and is overwritten), this is
    the first trusted init: ANY ``grant.json`` present at this instant was on disk
    before the gateway ever drew the boundary under the mask, so it is a pre-upgrade
    plant and is QUARANTINED here (see :func:`_quarantine_plant`) before the marker is
    written. A grant written AFTER the boundary -- which, on this masked version, only
    the operator can do, since the agent cannot reach the leaf -- is honoured.

    *requested_mode* is the live sandbox tier the caller resolved; the boundary is
    drawn only where it masks the keystone. Idempotent and race-safe: ``O_EXCL`` makes
    exactly one concurrent first init win, and the quarantine runs only on the branch
    that draws a fresh marker, so a loser neither re-quarantines nor refreshes the
    boundary. Failure is logged and swallowed -- a gateway that cannot draw the boundary
    must not abort boot; the read path fails closed (a grant with no authentic marker
    is quarantined), so a missing marker withholds the grant rather than granting one.
    """
    if not _keystone_is_masked(requested_mode):
        # The mask is not in force, so this is not a trusted boundary to draw: an
        # unmasked host grants nothing (`is_declared` refuses), and drawing a marker
        # here would let a later masked startup trust a boundary set while the leaf
        # was reachable. Leave the state untouched.
        return
    marker = standing_approval_init_marker_path()
    marker_present = marker.parent.is_dir() and marker.exists()
    if marker_present and _marker_is_authentic():
        # A genuine regular single-link marker is already in place -- this gateway drew
        # it on a prior masked boot, after which the directory has stayed masked, so a
        # grant present now is a post-boundary operator write. Nothing to redraw or
        # quarantine. (The plain-file residual below is why presence alone is not
        # trusted on the FIRST masked boot; after it, the mask keeps the dir honest.)
        return
    try:
        marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # mkdir(exist_ok=True) does not chmod an ALREADY-present directory, and the
        # sandbox materializer `continue`s on an existing dir without chmod, so a dir
        # created earlier under umask (0755) would stay group/other-readable and leak
        # the grant to other local users. Tighten it to 0700 explicitly. Best effort:
        # a chmod failure does not abort the boundary draw (the mask, not the mode bits,
        # is the security boundary against the agent; this hardens against local peers).
        try:
            # 0o700 on a DIRECTORY is owner-only -- tighter than the Semgrep rule's 0o644
            # advice, which would strip traverse and widen read to group/other. Tightening
            # it IS the hardening here, so the insecure-file-permissions rule is a false
            # positive on this line.
            os.chmod(str(marker.parent), 0o700)  # nosemgrep
        except OSError:
            logger.warning(
                "standing auto-approve keystone directory could not be tightened to "
                "0700; it is readable by other local users -- fix its mode by hand",
                exc_info=True,
            )
        # No authentic marker yet: this is the first masked boundary draw this gateway
        # can vouch for, so a grant present now was on disk before it -- a pre-upgrade
        # plant -- and is quarantined BEFORE the marker is drawn, so the window in which
        # a plant could be read as a grant never opens on this startup.
        if _read_all().get(GRANT_FIELD) is True:
            if not _quarantine_plant():
                # The plant could not be moved aside (e.g. a planted
                # grant.json.quarantined directory blocks os.replace). Drawing the
                # marker now would authenticate the surviving plant on the NEXT boot,
                # so leave the boundary UNdrawn: the read path stays fail-closed (no
                # authentic marker) and the plant keeps being refused until the
                # operator clears it.
                return
        # Replace any non-authentic (symlink/alias/multi-link) marker with a genuine
        # one. O_EXCL first (the fresh-install path); if a marker occupies the name,
        # unlink it and retry so the gateway owns the inode.
        try:
            fd = os.open(str(marker), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                os.unlink(str(marker))
            except OSError:
                pass
            fd = os.open(str(marker), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, b"1\n")
        finally:
            os.close(fd)
    except OSError:
        logger.warning(
            "standing auto-approve first-trusted-init boundary could not be drawn; "
            "a declared grant stays suspended until it can be",
            exc_info=True,
        )


def _quarantine_plant() -> bool:
    """Move a pre-init ``grant.json`` aside and log one security event.

    Called when a grant is present but predates this gateway's first trusted init.
    The document is renamed to ``grant.json.quarantined`` so it is never read again as
    an authorization and the operator can inspect what was planted; a fresh,
    deliberate re-declaration after init is honoured normally.

    Returns ``True`` only when the plant was actually moved aside. A ``False`` return
    means the rename failed -- e.g. a pre-upgrade agent planted a ``grant.json.quarantined``
    DIRECTORY (one ``mkdir``) so ``os.replace`` cannot clobber it -- and the plant is
    still on disk. The caller MUST NOT draw the trusted-init marker in that case: a
    drawn marker is authentic on the next boot, so writing it over a surviving plant
    would make the following startup honour the plant as a grant. On this boot the read
    path still fails closed (no authentic marker yet), so the grant is refused either
    way; the return value protects the NEXT boot.
    """
    path = standing_approval_path()
    quarantined = path.with_name(path.name + ".quarantined")
    logger.warning(
        "SECURITY: standing auto-approve keystone predates this gateway's first "
        "trusted initialization, so it is a pre-upgrade plant rather than an operator "
        "grant; refusing it and quarantining it to %s. Re-declare the grant by writing "
        "%s again after this version has started.",
        quarantined,
        path,
    )
    try:
        os.replace(str(path), str(quarantined))
        return True
    except OSError:
        logger.warning(
            "standing auto-approve plant could not be quarantined; it is refused "
            "regardless, and the trusted-init marker is NOT drawn so the next boot "
            "does not honour it. Remove %s by hand",
            path,
            exc_info=True,
        )
        return False


def _keystone_predates_trusted_init() -> bool:
    """Whether the grant on disk predates this gateway's first trusted-init boundary.

    ``True`` -- the grant is a plant, refuse it -- when there is NO authentic
    gateway-written marker (:func:`_marker_is_authentic`). On this masked version the
    boundary is drawn by :func:`establish_trusted_init` BEFORE any grant could be
    read, and that same call quarantines whatever grant was present at first draw, so
    by the time a read reaches here a surviving grant was written after the boundary.

    The check is deliberately NOT an mtime comparison against the marker: both the
    grant and a planted marker are files a pre-upgrade agent can create and backdate
    with ``utime``, so comparing their mtimes certifies the plant. The trust root is
    instead the marker's PROVENANCE -- it exists, as a genuine regular single-link
    file, only because the masked gateway drew it, a state the unmasked pre-upgrade
    release and the in-sandbox agent on this version both cannot reach. ``True`` (treat
    as a plant) is the fail-closed answer whenever that provenance cannot be confirmed.
    """
    return not _marker_is_authentic()


def _keystone_is_masked(requested_mode: str) -> bool:
    """Whether the sandbox this host builds for an agent spawn covers the keystone.

    The document's authority rests on the mask, not on its own contents: an agent
    subprocess that can see the real data home creates the directory and writes the
    grant itself, and the next startup reads what that subprocess wrote as the
    operator's standing authority. So the grant counts only where the mask holding
    the leaf out of an agent's reach is actually in force.

    Two predicates in :mod:`sandbox` already answer when the mask is skipped, and each
    says in its own docstring that a control whose security argument depends on the mask
    must not carry a copy of that reasoning. This composes them rather than re-spelling
    them, so a future branch that skips the mask is a change to them and reaches this
    grant for free:

    * :func:`sandbox.credential_mask_applies` answers whether ``wrap_argv`` would thread
      the hidden-dir set through the backend it selects for *requested_mode*. It owns the
      clamp to the governed floor, the ``off`` tier, the no-backend host, and the process
      already running inside a Crew sandbox -- that last one matters here because the
      outer sandbox deliberately leaves ``~/.aws``, ``~/.ssh`` and ``~/.kube`` readable
      and is not a substitute for an adapter-specific mask.
    * :func:`sandbox.spawn_delegates_masking` answers the different question that predicate
      cannot: on Windows, and on macOS with kiro-cli's internal sandbox enabled, the spawn
      is handed to kiro-cli. A backend is present, so the first predicate answers True, and
      yet none of Crew's masks are applied and the leaf is reachable from inside that
      confinement.

    What is left for this function is the one question neither of them asks: whether the
    keystone actually sits inside a path the masked target set names.

    **The platform consequence, stated rather than left to be discovered.** Crew's mask
    is a Linux bind mount or a macOS Seatbelt profile, so the standing grant is
    available on Linux with a working user namespace and on macOS where Crew's own
    Seatbelt runs. On Windows it is never available: there is no Windows backend for
    ``credential_mask_applies`` to find and the spawn is delegated there, so both
    predicates refuse. On macOS with kiro-cli's internal sandbox enabled it is likewise
    unavailable, because that configuration is exactly the delegation. This narrows what
    the declaration can do rather than what the retired config key could do -- that key
    grants nothing on ANY platform, so no host keeps a standing grant it could not hold.

    *requested_mode* is REQUIRED, and is the ``agent.sandbox`` value the caller already
    resolved: a startup must judge the document it is acting on rather than re-read
    configuration that may have moved underneath it, and every caller holds the value
    already, so a no-argument form would only be a second way to read it. It is passed
    through unclamped, because the clamp to the governed floor belongs to
    ``credential_mask_applies`` and applying it twice here would be the copy this
    function exists without: a floor raising a requested ``off`` to a confined tier is a
    masked host and keeps granting, and that predicate is where that is decided.

    ``normpath``, never ``realpath``: the target set is built with ``normpath`` for the
    reason :func:`sandbox._relocated_crew_targets` records, so a link-resolving syscall
    here could disagree with the rules the sandbox will actually install.
    """
    try:
        if not sandbox.credential_mask_applies(requested_mode):
            return False
        if sandbox.spawn_delegates_masking():
            return False
        leaf = os.path.normpath(str(standing_approval_path()))
        for target in sandbox._crew_hidden_sandbox_targets():
            masked = os.path.normpath(target)
            if leaf == masked or leaf.startswith(masked + os.sep):
                return True
        return False
    except Exception:
        logger.warning(
            "standing auto-approve keystone mask could not be established; "
            "treating approvals as required",
            exc_info=True,
        )
        return False


def is_declared(requested_mode: str) -> bool:
    """Whether the operator has declared a STANDING skip of every tool approval.

    Fails closed to ``False`` on a missing, unreadable or malformed document, which
    is what the issue's "an absent or unreadable leaf resolves to refusal" asks for.

    Only a real ``bool`` ``True`` counts. A truthy string or number does not, for the
    reason ``config.sections._read_skip_permissions`` gives about the key it replaces:
    ``"false"``, ``"0"`` and ``"no"`` are all truthy in Python, so a bare ``bool(...)``
    here would read an explicit disable as the standing grant. LOCAL only -- no network.

    A declared document is honoured only where :func:`_keystone_is_masked` holds, so an
    agent subprocess that can reach the leaf cannot author its own standing authority.
    The mask is established only once the document already grants, so a host with no
    grant -- every default install -- pays nothing for the check and stays silent;
    a host that HAS one and cannot mask it says so, because an operator who wrote the
    document and still sees prompts needs the reason.

    **The pre-upgrade plant -- narrowed by a first-trusted-init boundary; one residual
    stated honestly.** The mask closes the ONGOING threat: on this version no agent
    subprocess can open or write the keystone, so no session can author its own standing
    authority. One bounded, one-time window remains: on a release BEFORE this leaf was
    masked the crew data-home root was agent-writable and nothing masked
    ``standing-approval/``, so a hostile pre-upgrade agent could plant ``grant.json`` =
    ``True`` and wait for an upgrade to read it back.

    :func:`establish_trusted_init` narrows that window. It draws a boundary marker
    (``standing-approval/.trusted-init``) from the gateway, only where
    :func:`_keystone_is_masked` holds, on the first masked startup. When no AUTHENTIC
    marker is present (:func:`_marker_is_authentic` -- a genuine regular single-link
    file reached through an ``O_NOFOLLOW`` / ``O_DIRECTORY`` parent pin), it quarantines
    any ``grant.json`` present to ``grant.json.quarantined`` with one security log event
    BEFORE drawing the marker, and it redraws a genuine marker over any symlinked,
    aliased or multi-linked plant. This closes every plant shape EXCEPT one: a plant
    that writes a plain regular ``.trusted-init`` beside its ``grant.json`` on the exact
    first masked boot after upgrade. That marker is byte-identical to a gateway-written
    one, so presence alone cannot tell boot-1-plant from a legitimate boot-N marker, and
    on boot N the co-present operator grant must NOT be quarantined -- so the first-boot
    plant with a matching plain marker survives. The residual is irreducible by any
    on-disk artifact: at the first masked boot EVERY file in the (then-unmasked)
    directory is attacker-controllable, so no on-disk marker, mtime, nonce or key can be
    authenticated as the gateway's rather than the plant's. Closing it needs a trust
    root the pre-upgrade agent never held -- a gateway secret minted AFTER the plant
    window and stored where the new version masks it -- which is out of this change's
    scope and is tracked separately.

    Three earlier revisions tried to narrow the window and each was the wrong trade: an
    on-disk first-boot marker read against the GRANT alone buys nothing, because the
    pre-upgrade attacker that plants ``grant.json`` can plant one more file to satisfy
    it; an in-memory live-observation layer refused a grant merely PRESENT at boot,
    which killed the standing grant on every ordinary restart; and comparing the marker
    and grant MTIMES certified the plant, because ``utime`` on two attacker-written files
    is free. The boundary here does not refuse a boot-present grant (a post-init operator
    grant survives every restart, because the authentic marker persists) and defeats the
    symlink/alias/multi-link and no-marker plant shapes; the plain-marker first-boot
    residual above is what it does not reach. An operator who re-declares the grant once
    after upgrading keeps it as standing authority with no further action.

    *requested_mode* is forwarded to :func:`_keystone_is_masked` and is required: pass
    the sandbox tier IN FORCE NOW, resolved from :func:`config.live.current` immediately
    before activation -- never a boot copy cached at startup. The grant is honoured only
    where the live sandbox masks the keystone, so a mode that went stale while the gateway
    booted (an operator flips ``agent.sandbox`` off, or down to an unmasked tier, between
    startup and this call) must not be able to activate a grant the live host does not
    protect. The two startup paths (dashboard and Slack) read the live snapshot and pass
    its ``agent.sandbox`` here; a mid-flight unmask therefore refuses, leaving the grant
    suspended rather than activated under a mask that is not installed.
    """
    document = _read_all()
    if document.get(GRANT_FIELD) is not True:
        return False
    if not _keystone_is_masked(requested_mode):
        logger.warning(
            "standing auto-approve is declared on the keystone but is UNAVAILABLE on "
            "this host: Kiro Crew's own sandbox does not mask that leaf away from the "
            "agent here, so the document is not an authorization. Approvals are required"
        )
        return False
    if _keystone_predates_trusted_init():
        # The grant was on disk before this gateway's first trusted init under the
        # mask, so it is a pre-upgrade plant, not an operator grant: quarantine it and
        # refuse. A fresh re-declaration after init is newer than the marker and is
        # honoured on the next startup.
        _quarantine_plant()
        return False
    # The mask holds and the grant is a fresh post-init operator write: an agent
    # subprocess cannot reach the keystone on this version, and the first-trusted-init
    # marker proves this grant was established after the gateway ran masked. The grant
    # is an authorization and is honoured on every startup.
    return True


def migration_notice(
    requested_mode: str,
    *,
    masked: bool | None = None,
) -> str:
    """The words a startup logs when a retired ``config.json`` declaration is stranded.

    Returned rather than logged here so the two startup paths that establish the
    declared grant (the dashboard's and Slack's) word it identically, and so a test
    can assert on the text an operator actually sees instead of on a log call.

    The whole value of this notice is that the person reading it can act on it without
    going to find the documentation first, so it names the resolved path and the one
    line to put in it. It deliberately does NOT hand over a runnable redirection
    command on any platform:

    * It does not spell the path as ``$KIROCREW_HOME/...``. That variable is unset on a
      default installation, where the data home comes from ``config_dir()``, so a shell
      expands the env-var form to ``/standing-approval`` and a command using it fails at
      the filesystem root.
    * It does not hand a Windows operator a POSIX one-liner. ``mkdir -p`` and ``printf``
      are not commands there, and POSIX single quotes are not quoting characters to
      ``cmd`` at all -- the same trap :func:`sandbox._delete_file_command` records.
    * It does not hand a POSIX operator a ``printf ... > path`` redirection either. A
      pre-upgrade agent can plant a symlink at the keystone path (the crew data-home
      root is agent-writable), and an operator who copy-pastes such a line would follow
      that symlink and overwrite the attacker's chosen target with operator privilege.
      So BOTH renderings name the path and the exact one line and ask the operator to
      create the file by hand -- no redirection this notice emits can be turned against
      them.

    The remedy is the SAME on every platform, so the wording is written once and
    resolved here rather than branched: *masked* selects the rendering, and
    *requested_mode* -- required, like :func:`is_declared`'s -- is what resolves it when
    *masked* is not given.

    **There are two renderings, and which one is right depends on the mask.** Where
    :func:`_keystone_is_masked` holds, writing the document restores the grant: the notice
    hands over the path and the exact one line to write, and the grant is honoured on the
    next restart. Where the mask does not hold, writing the document restores NOTHING --
    :func:`is_declared` refuses a declaration whose mask is absent -- so a remedy there
    would send the operator to create a file, watch the prompts continue, and have nothing
    further to read. That rendering states the unavailability and points at the ad-hoc
    duration, which is the grant that does work on such a host. It is also the correct text
    when the keystone is ALREADY written on an unmasked host, which is the case where a
    remedy reads most absurdly.
    """
    on_masked = _keystone_is_masked(requested_mode) if masked is None else masked
    path = standing_approval_path()
    document = f'{{"{GRANT_FIELD}": true}}'
    if not on_masked:
        return (
            "agent.dangerously_skip_permissions is set in config.json but that key no "
            "longer grants anything, and the standing auto-approve declaration it moved "
            f"to ({path}) is UNAVAILABLE on this host: the declaration is honoured only "
            "where Kiro Crew's own sandbox masks that leaf away from the agent, and this "
            "host's agent spawn runs without that mask. Writing the document would grant "
            "nothing. Approvals are REQUIRED here; enable auto-approve ad hoc instead "
            "(it lasts agent.yolo_duration), and remove the retired key from config.json."
        )
    preamble = (
        "agent.dangerously_skip_permissions is set in config.json but that key no "
        "longer grants anything: the standing auto-approve declaration moved to the "
        f"operator-owned keystone {path}, which an agent sandbox cannot open. "
        "Approvals are REQUIRED until you write it. "
    )
    # Deliberately NOT a runnable redirection command. A pre-upgrade agent can plant a
    # symlink at ``path`` (the crew data-home root is agent-writable), and an operator who
    # copy-pastes a ``printf ... > path`` line would follow that symlink and overwrite the
    # attacker's chosen target with operator privilege. The notice names the path and the
    # exact one-line content and asks the operator to create the file by hand, so no
    # redirection this notice emits can be turned against them.
    remedy = (
        f"To restore the grant, create the directory {path.parent} if it does not exist, "
        f"then create the file {path} by hand containing exactly this one line: "
        f"{document} . Restart Kiro Crew and the grant is honoured. "
    )
    return preamble + remedy + "(then remove the retired key from config.json)."
