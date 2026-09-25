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
import shlex
import stat
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.config.loader import standing_approval_path

logger = logging.getLogger(__name__)

#: The field inside the keystone document. Spelled the same as the retired
#: ``config.json`` key on purpose: an operator moving the declaration copies one line
#: rather than learning a second name for the same switch.
GRANT_FIELD: str = "dangerously_skip_permissions"

#: Size bound for the grant document. It holds one boolean and an optional timestamp,
#: so a few hundred bytes is generous; anything larger is not a legitimate grant. The
#: bound is what stops a huge file at this name from being buffered into the gateway's
#: memory on a synchronous boot read.
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

    * ``O_NOFOLLOW`` (POSIX): the open itself refuses a symlink final component,
      race-free where an ``is_symlink()`` pre-check is not.
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

    Returns ``None`` on any refusal or I/O error, so every caller fails closed. The
    descriptor's release counts as part of the read: a failing ``close`` is caught here
    and resolves to no grant, because this reader runs on the gateway's startup thread
    outside any ``try``, so an error escaping it would abort boot instead of withholding
    a grant.
    """
    path = standing_approval_path()
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    pre: os.stat_result | None = None
    try:
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
    return None if len(data) > _MAX_GRANT_BYTES else data


def _read_all() -> dict[str, Any]:
    """The whole document, or ``{}`` when it is missing, refused or unreadable.

    Failing soft is the right READ behaviour: an authorization record that cannot be
    parsed is not an authorization, so the grant stays withheld and the session
    prompts. An absent document, a refused shape and an unparseable one resolve
    identically, which is also what makes the empty mask a sandboxed reader would see
    equal to no grant.
    """
    raw_bytes = _read_grant_bytes()
    if raw_bytes is None:
        return {}
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
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
        from kiro_crew import sandbox

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

    *requested_mode* is forwarded to that predicate and is required: pass the
    ``agent.sandbox`` value already in hand.
    """
    declared = _read_all().get(GRANT_FIELD) is True
    if declared and not _keystone_is_masked(requested_mode):
        logger.warning(
            "standing auto-approve is declared on the keystone but is UNAVAILABLE on "
            "this host: Kiro Crew's own sandbox does not mask that leaf away from the "
            "agent here, so the document is not an authorization. Approvals are required"
        )
        return False
    return declared


def migration_notice(
    requested_mode: str,
    *,
    windows: bool | None = None,
    masked: bool | None = None,
) -> str:
    """The words a startup logs when a retired ``config.json`` declaration is stranded.

    Returned rather than logged here so the two startup paths that establish the
    declared grant (the dashboard's and Slack's) word it identically, and so a test
    can assert on the text an operator actually sees instead of on a log call.

    The whole value of this notice is that the person reading it can act on it without
    going to find the documentation first, so it names the resolved path and, on POSIX,
    a command that RUNS. Two things it deliberately does not do:

    * It does not spell the path as ``$KIROCREW_HOME/...``. That variable is unset on a
      default installation, where the data home comes from ``config_dir()``, so a shell
      expands the env-var form to ``/standing-approval`` and the command fails at the
      filesystem root.
    * It does not hand a Windows operator a POSIX one-liner. ``mkdir -p`` and ``printf``
      are not commands there, and POSIX single quotes are not quoting characters to
      ``cmd`` at all -- the same trap :func:`sandbox._delete_file_command` records. No
      single spelling writes this document in both ``cmd`` and PowerShell, because the
      content itself contains the ``"`` that is their quote character, so the Windows
      rendering states the path and the one line to put in it rather than pretending to
      be runnable.

    *windows* defaults to this host and exists as a PARAMETER so BOTH renderings are
    exercisable from one platform, which is the remedy its sibling helper names: every
    platform defect in this area came from a string written once, verified where it was
    written, and asserted as general. *masked* is a parameter for the same reason, and
    *requested_mode* -- required, like :func:`is_declared`'s -- is what resolves it when
    *masked* is not given.

    **There are two renderings, and which one is right depends on the mask.** Where
    :func:`_keystone_is_masked` holds, writing the document restores the grant and the
    notice hands over the way to write it. Where it does not, writing the document
    restores NOTHING -- :func:`is_declared` refuses a declaration whose mask is absent --
    so a remedy there would send the operator to create a file, watch the prompts
    continue, and have nothing further to read. That rendering states the unavailability
    and points at the ad-hoc duration, which is the grant that does work on such a host.
    It is also the correct text when the keystone is ALREADY written on an unmasked host,
    which is the case where a remedy reads most absurdly.
    """
    on_windows = platform_compat.IS_WINDOWS if windows is None else windows
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
    if on_windows:
        remedy = (
            f"To restore the grant, create the file {path} containing this one line: "
            f"{document}  "
        )
    else:
        remedy = (
            f"To restore the grant, run: mkdir -p {shlex.quote(str(path.parent))} && "
            f"printf '{document}\\n' > {shlex.quote(str(path))}  "
        )
    return preamble + remedy + "(then remove the retired key from config.json)."
