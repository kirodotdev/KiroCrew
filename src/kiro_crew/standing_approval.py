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
for it in the writable root, write the standing value through that name, and unlink
it -- and the next startup reads that changed document under a single link.

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
keeps the standing grant: the key is a DEPRECATED ALIAS that still grants on every
platform, so no one loses the grant on upgrade. The deprecation is announced rather
than silent: :func:`migration_notice` returns the words a startup logs when the key is
set and the keystone is absent, naming the keystone to move to. The key is slated to
stop granting in a future release, which needs a recorded decision first.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from typing import Any

from kiro_crew import sandbox
from kiro_crew.config.loader import standing_approval_path

logger = logging.getLogger(__name__)

#: The field inside the keystone document. Spelled the same as the deprecated
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
    nothing, while a second name for THIS one is the whole concern. Making the refusal
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
    the declaration can do rather than what the deprecated config key does -- that key
    is a backwards-compatible alias that still grants on every platform.

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

    **One residual, stated honestly.** The mask closes the ONGOING threat: on this
    version no agent subprocess can open or write the keystone, so no session can author
    its own standing authority. One bounded, one-time window remains: on a release BEFORE
    this leaf was masked the crew data-home root was agent-writable and nothing masked
    ``standing-approval/``, so a pre-upgrade agent could write ``grant.json`` =
    ``True`` and an upgrade would read it back. That residual is irreducible by any
    on-disk artifact -- at the first masked boot every file in the then-unmasked directory
    is agent-writable, so no on-disk marker, mtime or nonce can be authenticated as
    the gateway's rather than one the agent wrote -- and closing it needs a trust root the
    pre-upgrade agent never held (a gateway secret minted after the window), which
    is out of this change's scope and tracked separately. This PR was SHRUNK per the audit
    to the minimal masked-leaf move; the earlier first-trusted-init marker/quarantine
    machinery was removed because it could not close that residual either and added an
    on-disk trust root that the agent could itself write.

    *requested_mode* is forwarded to :func:`_keystone_is_masked` and is required: pass
    the sandbox tier IN FORCE NOW, resolved from :func:`config.live.current` immediately
    before activation -- never a boot copy cached at startup. The grant is honoured only
    where the live sandbox masks the keystone, so a mode that went stale while the gateway
    booted (an operator flips ``agent.sandbox`` off, or down to an unmasked tier, between
    startup and this call) must not activate a grant the live host does not protect. The
    two startup paths (dashboard and Slack) read the live snapshot and pass its
    ``agent.sandbox`` here, so a mid-flight unmask refuses rather than granting under a
    mask that is not installed.
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
    # The mask holds and the grant is a real boolean True: an agent subprocess cannot
    # open or write the keystone on this version, so the document is the operator's
    # standing authorization and is honoured on every startup.
    return True


def migration_notice(
    requested_mode: str,
) -> str:
    """The words a startup logs when a DEPRECATED ``config.json`` declaration is in use.

    The config key still grants (First-Principles ruling: it is not retired and platforms
    are not narrowed), so this notice tells the operator the key is deprecated and names
    the keystone to migrate to for the secure, mask-protected home. Returned rather than
    logged here so the two startup paths that establish the declared grant (the
    dashboard's and Slack's) word it identically, and so a test can assert on the text an
    operator actually sees instead of on a log call.

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
      crew data-home root is agent-writable, so a symlink could sit at the keystone path,
      and an operator who copy-pastes such a line would follow that symlink and overwrite
      whatever it points at with operator privilege.
      So BOTH renderings name the path and the exact one line and ask the operator to
      create the file by hand -- no redirection this notice emits can be turned against
      them.

    The remedy is the SAME on every platform, so the wording is written once and
    resolved here rather than branched: *requested_mode* -- required, like
    :func:`is_declared`'s -- is what selects the rendering, through
    :func:`_keystone_is_masked`.

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
    on_masked = _keystone_is_masked(requested_mode)
    path = standing_approval_path()
    document = f'{{"{GRANT_FIELD}": true}}'
    if not on_masked:
        return (
            "agent.dangerously_skip_permissions is set in config.json. It still grants "
            "the standing auto-approve, but it is DEPRECATED: the secure home for this "
            f"declaration is the operator-owned keystone {path}, which an agent sandbox "
            "cannot open. On THIS host, however, Kiro Crew's own sandbox does not mask "
            "that leaf away from the agent, so the keystone cannot be honoured here and "
            "the config key remains the only standing path for now. Nothing to change on "
            "this host; the config key keeps working."
        )
    preamble = (
        "agent.dangerously_skip_permissions is set in config.json. It still grants the "
        "standing auto-approve, but it is DEPRECATED: the standing declaration should "
        f"move to the operator-owned keystone {path}, which an agent sandbox cannot open "
        "(the config key is an agent-readable document that can be reached through a second "
        "name, which is why it is being replaced). "
    )
    # Deliberately NOT a runnable redirection command. The crew data-home root is
    # agent-writable, so a symlink could sit at ``path``, and an operator who copy-pastes a
    # ``printf ... > path`` line would follow that symlink and overwrite whatever it points
    # at with operator privilege. The notice names the path and the exact one-line content
    # and asks the operator to create the file by hand, so no redirection this notice emits
    # can resolve anywhere unexpected.
    remedy = (
        f"To migrate, create the directory {path.parent} if it does not exist, then "
        f"create the file {path} by hand containing exactly this one line: {document} . "
        "Restart Kiro Crew and the keystone grant is honoured. "
    )
    return preamble + remedy + "(then remove the deprecated key from config.json)."
