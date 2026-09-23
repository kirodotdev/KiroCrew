"""Where the LAUNCH's own state lives, separate from the operator's configuration.

``cloud.json`` is hand-edited: an operator writes the ``fargate`` block into it, and there is
no wizard step and no dashboard form for that block today. The launch path's own bookkeeping --
which profile and region the last launch used, and the tag naming the stack it created -- was
written back into that same file, and every collision between the two owners followed from
that one fact. A post-deploy write into a file a person may have left mid-edit has to choose
between overwriting their bytes and refusing; refusing aborts the command after the deploy is
already billed, and overwriting loses their block.

So the two owners get two files. This one is product-owned: nothing hand-edits it and the launch
path is the only writer. A READ of it does feed a security decision -- the tag in it is the
default target ``kirocrew cloud destroy`` deletes when no ``--tag`` is given -- which is why it
is sealed and why an alias-backed copy is refused, both described below. ``cloud.json`` becomes
read-only to the product -- `CloudConfig` has no writer at all -- so there is no post-deploy
write into the operator's file to refuse, nothing to clobber, and no cross-process lock to hold
while doing it.

**Sealed against agent writes, and refused when aliased.** The pointer is an input to a
security decision: it is the tag ``kirocrew cloud destroy`` resolves when no ``--tag`` is given,
and ``--yes`` is exactly the path that does not stop to describe what it found. So three layers
cover the PATH -- the agent file-write gate, the kernel read-only seal, and pre-creation so an
absent name cannot be squatted -- and all three name a path, which a second name for the same
inode is not. :meth:`LaunchState.load` therefore refuses an alias-backed record where a command
consumes it. ``cloud.json`` keeps a separate reason of its own: ``fargate.image`` chooses which
container receives the model credential.

**Read-through for existing installs.** An install that launched before this file existed has
its pointer in ``cloud.json``. :meth:`LaunchState.load` falls back to those fields when this
file has nothing, so ``kirocrew cloud resume`` re-attaches exactly as it did. The fallback is
read-only: nothing migrates the value by writing, because writing is the thing being removed.
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.cloud.config import DEFAULT_REGION, CloudConfig, tag_is_wellformed
from kiro_crew.config.loader import config_dir
from kiro_crew.sandbox import (
    SandboxCeilingUnsealable,
    require_unaliased_cloud_config,
    require_unaliased_launch_state,
)

logger = logging.getLogger(__name__)

#: The product-owned launch record, beside ``cloud.json`` in the crew home.
_FILENAME = "cloud_launch_state.json"

#: Same ceiling the configuration reader uses. This document holds three short strings, so
#: anything near it is not a record; the bound is here because a reader that trusts a file's
#: size is a reader an unbounded file can exhaust.
_MAX_FILE_BYTES = 64 * 1024

#: ``O_NOFOLLOW`` where the platform has it, else nothing. Absent on Windows, which is a
#: platform ``kirocrew cloud`` really runs on, so this is a ``getattr`` rather than a direct
#: use. Where it IS present it refuses a symlinked leaf in the kernel, which is stronger than
#: any check after the open; where it is absent the by-name refusal in
#: :meth:`LaunchState.load` is what still reports that shape.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

#: ``O_BINARY`` where the platform has it. Windows-only, and required there or the read
#: translates CRLF and the byte ceiling stops counting the bytes that are on disk.
_O_BINARY = getattr(os, "O_BINARY", 0)


def state_path() -> Path:
    """The launch record's path, honouring ``KIROCREW_HOME`` through ``config_dir()``."""
    return config_dir() / _FILENAME


@dataclass(frozen=True)
class LaunchState:
    """What the launch path knows about the deployment it last created.

    Frozen: a caller that wants to change a field writes a new record through
    :meth:`record`, so there is no load-mutate-save shape to get wrong and no object whose
    in-memory edits silently fail to reach disk.
    """

    profile: str = ""
    region: str = DEFAULT_REGION
    last_tag: str = ""

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "LaunchState":
        """This install's launch record, from this file or from the legacy fields.

        Tolerant about CONTENT, like the configuration reader and for the same reason: a
        cloud command must not hand an operator a traceback over a file it can simply treat
        as unset. Every way the document can fail to be one answers the same thing -- there
        is no record here -- and the legacy fields are then consulted, which is what keeps
        ``cloud resume`` working on an install that predates this file.

        Refusing about the NAME, which is the opposite call and not a contradiction. A
        malformed document is a file with nothing in it worth acting on; a file reachable
        under a second name is a file whose tag an agent can choose, and the tag decides
        which stack ``cloud destroy --yes`` deletes. Treating that as "no record" would
        hand back the legacy tag and carry on, so it refuses instead, and refuses HERE
        rather than in each verb: this read is what every tag-consuming command and the
        wizard's re-attach both go through, and a second consume point appearing later
        would otherwise be unguarded with nothing saying so.

        The whole record refuses, not just the tag. Which field a caller happens to want
        does not make the file trustworthy -- a forged profile and region point ``cloud
        list`` at an account where the operator's instance is not found, and a second
        launch is the expensive thing they do next.
        """
        p = path or state_path()
        require_unaliased_launch_state(str(p))
        return cls._load(p, guard_legacy=True, pin_record=True)

    @classmethod
    def _load_unguarded(cls, p: Path) -> "LaunchState":
        """The read with no alias refusal. For the POST-DESTROY clear only.

        :meth:`clear_tag` runs AFTER ``destroy`` has already deleted the stack, so nothing it
        does may raise: a refusal there would abort a command whose irreversible work is
        done, which is the failure shape this module exists to have removed. The tag it
        compares against came from a consume point that already refused an aliased file, so
        the check is not skipped -- it happened earlier, where its answer could still change
        what the command did.

        That argument is about WHEN the caller runs, and it does not extend to every clear.
        :meth:`try_clear_tag` runs before provisioning for ``wizard._clear_prior_pointer``,
        where an abort costs no stack and no bill, so that path guards; see its own docstring.
        This helper is reached only through the ``require_unaliased=False`` opt-out.
        """
        return cls._load(p, guard_legacy=False)

    @classmethod
    def _load(cls, p: Path, *, guard_legacy: bool, pin_record: bool = False) -> "LaunchState":
        """The document, or the legacy fields when it is not a record.

        *guard_legacy* is what tells the two entry points apart, and it exists because the
        FALLBACK reads a different file with its own alias refusal. Guarding the record and
        then falling through to an unguarded ``cloud.json`` left the hole open through the old
        file: an aliased configuration plus an empty record -- which is the state the sandbox
        pre-creates, so it is the DEFAULT on any install that has spawned an agent -- hands a
        forged ``last_tag`` to ``cloud destroy`` and the wrong stack is deleted. The guard
        belongs on both files because either one can be the one the tag came from.
        """
        data = _read_document(p, pin=pin_record)
        # A document carrying NONE of the three keys is not a record -- it is the empty
        # ``{}`` the sandbox pre-creates so its read-only seal has a file to bind to, and it
        # has to mean what an absent file means or every install whose pointer still lives in
        # ``cloud.json`` would read as "no previous launch" the moment an agent spawned.
        #
        # Keyed on the KEYS being present, not on the tag being non-empty: ``destroy`` writes
        # a record whose ``last_tag`` is deliberately ``""``, and falling back on an empty tag
        # would resurrect a pointer to the stack it just deleted from the old file.
        if data is not None and any(k in data for k in ("profile", "region", "last_tag")):
            tag = str(data.get("last_tag", ""))
            return cls(
                profile=str(data.get("profile", "")),
                region=str(data.get("region", "") or DEFAULT_REGION),
                # Sanitised at the boundary exactly as the configuration reader does it: a
                # malformed tag must not reach the resume path, where `validate_tag` raises,
                # and an empty one already means "no last launch".
                last_tag=tag if tag_is_wellformed(tag) else "",
            )
        # Falling through to the OPERATOR's file. While it is being read for a tag it selects
        # the destroy target exactly as the record does, so it gets the same refusal its own
        # launch seam gives it. That seam (``defaults.engine_for``) runs on a LAUNCH and never
        # on a teardown, so without this line the destroy path reached those fields with no
        # alias check anywhere between the file and the deletion.
        if guard_legacy:
            require_unaliased_cloud_config()
        legacy = CloudConfig.load()
        return cls(profile=legacy.profile, region=legacy.region, last_tag=legacy.last_tag)

    @classmethod
    def record(
        cls,
        *,
        profile: str,
        region: str,
        last_tag: str,
        path: Optional[Path] = None,
    ) -> None:
        """Write the launch record, replacing whatever was there.

        A whole-record write rather than a merge, because this file has ONE writer and three
        fields that a launch decides together: the profile and region it deployed into and the
        tag it created. No other party has fields here to lose, which is exactly why the
        launch path writes this file and not the operator's.
        """
        p = path or state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with _writer_lock(p):
            _write_record(p, profile=profile, region=region, last_tag=last_tag)

    @classmethod
    def clear_tag(cls, expect: str, path: Optional[Path] = None) -> bool:
        """Clear ``last_tag`` only while it still names *expect*. Answers whether it did.

        ``destroy`` owns this pointer for the stack it just deleted and for no other. Read and
        cleared unconditionally, a launch that recorded its own tag in between would have its
        pointer wiped by a command that never saw it.

        The read, the compare and the write happen under ONE hold of the writer lock. As three
        separate steps the compare answers about a file that has moved on by the time the write
        lands: a launch recording its tag between them is overwritten by the clear, and the
        pointer to a live instance is gone. The lock is what leaves no window between them.

        The clear does NOT refuse an aliased record, and that is scoped to this method's one
        caller rather than to clears in general: it runs after ``destroy`` has deleted the
        stack, so there is no decision left for a refusal to change and aborting would only
        turn a completed removal into a traceback. :meth:`try_clear_tag` is the same operation
        for a caller that runs BEFORE its irreversible step, and it refuses.
        """
        return cls.try_clear_tag(expect, path=path, require_unaliased=False)[0]

    @classmethod
    def try_clear_tag(
        cls, expect: str, path: Optional[Path] = None, *, require_unaliased: bool = True
    ) -> "tuple[bool, str]":
        """Clear ``last_tag`` while it still names *expect*, and answer what is saved after.

        The same operation as :meth:`clear_tag`, reporting the tag the compare actually saw
        rather than only whether it matched. A caller that has to DECIDE something on a
        declined clear needs that, because the bool covers two states and only one is a
        hazard: it declines when another launch recorded its own tag, and equally when there
        is no pointer at all. Provisioning is unsafe in the first case and perfectly safe in
        the second.

        The tag comes from inside the SAME hold of the writer lock as the compare. Reading the
        record again afterwards would answer about a file that can have moved on between the
        two reads, and would also be answered by a stubbed reader rather than by the file the
        compare used.

        **This read refuses an alias-backed record, and *require_unaliased* defaults to True
        so a consume point added later inherits the refusal instead of the exemption.** The
        record's tag is an input to a security decision here exactly as it is in
        :meth:`load`: ``wizard._clear_prior_pointer`` provisions or aborts on what this
        reports, and a forged EMPTY pointer turns a deliberate abort into a launch that leaves
        a live stack named by a pointer its operator never wrote -- which is the state a later
        ``destroy`` with no ``--tag`` resolves. The refusal is affordable here for the same
        reason that abort is: nothing has been provisioned and nothing is billing.
        ``cli_cloud``'s dispatch already catches ``SandboxCeilingUnsealable`` for every verb,
        so this prints the file, the shape and the remedy rather than a traceback.

        *require_unaliased=False* is the post-destroy opt-out and has exactly one caller,
        :meth:`clear_tag`, whose stack is already gone.
        """
        p = path or state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with _writer_lock(p):
            if require_unaliased:
                require_unaliased_launch_state(str(p))
                current = cls._load(p, guard_legacy=False, pin_record=True)
            else:
                current = cls._load_unguarded(p)
            if current.last_tag != expect:
                return False, current.last_tag
            _write_record(p, profile=current.profile, region=current.region, last_tag="")
        return True, ""


def _write_record(p: Path, *, profile: str, region: str, last_tag: str) -> None:
    """Publish the record atomically.

    Takes NO lock: both callers already hold one, and a nested acquire would either deadlock
    or, worse, quietly succeed and leave ``clear_tag``'s compare and write in two different
    critical sections -- the exact window the lock exists to close.

    All three keys are always written, which is what lets :meth:`LaunchState.load` tell a real
    record from the empty ``{}`` the sandbox pre-creates. A record whose tag is ``""`` still
    carries the key, so a cleared pointer stays cleared instead of falling back to the legacy
    fields in ``cloud.json``.
    """
    atomic_write(
        p,
        json.dumps({"profile": profile, "region": region, "last_tag": last_tag}, indent=2) + "\n",
    )


def _refuse_or_absent(p: Path, exc: OSError, *, pin: bool) -> None:
    """``None`` for the exempt caller, a refusal for the pinned one.

    The ONE place that rule is spelled, and it is a function rather than two matching ``except``
    bodies for a measured reason: the first version of this module applied it to the ``open``
    and not to the ``read``, because they were separate handlers and only one of them had been
    written. A review caught the read side still answering "no record" on the pinned path --
    reachable by swapping a directory in, since ``open`` on a directory succeeds and the ``read``
    then fails with ``EISDIR``. With the rule in one callable there is no second copy to forget.
    """
    if not pin:
        return None
    raise SandboxCeilingUnsealable(_unreadable_record_detail(p, exc)) from exc


def _unreadable_record_detail(p: Path, exc: OSError) -> str:
    """The refusal for a pinned read that could not open the record at all.

    Names the errno, because the two shapes an operator can act on are very different and the
    message is the only thing they get: ``ELOOP`` is a symlink at the leaf, which their own
    dotfile manager or backup tool probably left and which they can undo; anything else is a
    permission or device fault on a file the product owns.

    Says why it refuses rather than reporting no record, because "no record" is the answer an
    attacker wants here: it is what makes a pre-provision clear read as "nothing saved".
    """
    code = errno.errorcode.get(exc.errno or 0, str(exc.errno))
    if exc.errno == errno.ELOOP:
        shape = (
            f"the launch record {p} is a SYMLINK, so the name stays replaceable in a writable "
            "directory while the seal binds whatever it resolves to"
        )
        remedy = "Replace the link with a regular file, then re-run."
    else:
        shape = f"the launch record {p} could not be opened ({code}: {exc.strerror})"
        remedy = "Fix the file's permissions or the device error, then re-run."
    return (
        f"{shape}. This read chooses which stack `kirocrew cloud destroy --yes` deletes and "
        "whether a launch may provision, so a read that cannot be judged is refused instead of "
        f"being reported as no previous launch. {remedy}"
    )


@contextmanager
def _writer_lock(p: Path) -> "Iterator[None]":
    """Serialise this file's two PRODUCT writers for one read-modify-write.

    Deliberately small, and NOT the machinery this module replaced. That one guarded the
    OPERATOR's configuration against a person editing it in a text editor -- a writer that
    takes no lock, which is why it needed a witness on every replace, a bounded acquire and a
    livelock bound on top. Here both writers are Crew's own, they both take this lock, and
    there are exactly two: the launch recording its tag and ``destroy`` clearing it. That is
    all an advisory lock has ever been able to do, and here it is enough.

    Without it ``clear_tag`` reads, compares and writes as three steps, so a launch that
    records its tag in between is overwritten by the clear -- the pointer to a live instance
    lost to a command that never saw it.

    ``platform_compat.file_lock``, NOT ``flock_compat.flock``. That shim exists to keep the
    import graph loadable on Windows and its ``flock`` is a NO-OP there, which is fine for the
    gateway machinery that never runs on Windows and wrong for this file: ``kirocrew cloud`` is
    exactly the command a Windows client runs, so the one platform the shim does nothing on is
    a platform where both writers are live. The compat lock serialises on all three -- POSIX
    through ``fcntl.flock``, Windows through ``msvcrt.locking`` on the lock file's first byte,
    which is why the lock lives in a dedicated ``.lock`` file rather than on the record itself.

    A lock that cannot be taken RAISES rather than letting the critical section run
    unserialised. Yielding anyway was the fail-open this lock exists to prevent: it left
    ``clear_tag``'s read, compare and write in three steps again, so a launch recording its
    tag in between had that pointer overwritten by a destroy that never saw it.

    Strict is safe here only because every writer's caller already answers an ``OSError``, and
    the two answers are the ones that make an unlockable mount harmless:

    * ``wizard._record_launch`` runs after a confirmed deploy and WARNS, so the instance is
      still reachable and only the pointer is missing;
    * ``wizard._clear_prior_pointer`` runs before provisioning and ABORTS, with nothing
      created and nothing billing;
    * ``cli_cloud._cloud_destroy``'s clear runs after the stack is gone and WARNS, leaving a
      pointer that is visibly stale.

    So a filesystem with no lock support degrades to "no pointer, with a warning", never to a
    failed command and never to a silently overwritten pointer. That is the opposite of the
    spawn-path refusal this PR downgraded, and for a concrete reason rather than a preference:
    there the cost of strictness was every sandboxed spawn on the host, with no caller in a
    position to absorb it. Here three callers already do.

    The lock file's own name is not sealed: an agent that unlinks it can make two legitimate
    writers race, but it cannot CHOOSE what either writes, which is what the seal on the
    record itself denies.
    """
    lock_path = p.with_name(p.name + ".lock")
    with contextlib.ExitStack() as stack:
        fh = stack.enter_context(open(lock_path, "a+"))
        stack.enter_context(platform_compat.file_lock(fh.fileno(), exclusive=True))
        yield


def _read_document(p: Path, *, pin: bool = False) -> "Optional[dict]":
    """The file as a JSON object, or ``None`` for every way it is not one.

    Reads at most one byte PAST the ceiling and decides from that, so an oversized file is
    never allocated. Reading it whole and checking the length afterwards is not a bound: the
    allocation has already happened by the time the check can look at it, and the file does not
    even have to be written a byte at a time to be enormous -- ``truncate -s`` makes a sparse
    one instantly.

    The extra byte is what makes the length check mean "the file is longer than the ceiling"
    rather than "the read stopped at the ceiling". Reading exactly the ceiling cannot tell those
    apart, so an oversized file whose first ``_MAX_FILE_BYTES`` bytes happen to be a complete
    record would pass the check and be adopted -- a truncated prefix read as the whole
    document.

    *pin* re-asks the alias question about the inode these BYTES came from, on the descriptor
    they were read through, and it is what stops the refusal being a check-then-use. The
    by-name check in :meth:`LaunchState.load` runs before this and judges whatever the name
    resolved to then; this one judges what was consumed. Both are wanted: the by-name call is
    the only one that can refuse a symlinked leaf and print its remedy, and this one is the
    only one that can say the judged inode is the inode that was read.

    Ordered AFTER the read, deliberately. Verifying first and reading second leaves the read
    unjudged, which is the shape being removed; verifying second means every alias present
    from this ``open`` through this ``read`` is refused, and the bytes a refusal was raised
    over are discarded rather than returned.

    ``O_NOFOLLOW`` is applied only WITH *pin*, so the exempt caller resolves the name exactly
    as it did before. It follows a symlinked record, which is what lets the post-destroy clear
    still clear a pointer on a host whose record is a link -- there is nothing left for a
    refusal to protect there, and declining to clear would leave the stale pointer the clear
    exists to remove. On the pinned path the flag refuses that shape in the kernel instead, so
    the descriptor handed to the alias check can never be a link's target.

    **Only ENOENT means "no record" on the pinned path.** Every other failure REFUSES there, and
    that covers the READ as well as the ``open`` -- both go through
    :func:`_refuse_or_absent`, which is the only place the rule is written. The distinction is
    the whole point rather than tidiness. ``O_NOFOLLOW`` reports a symlinked leaf as ``ELOOP``,
    and a directory swapped in at the name OPENS fine and then fails the read with ``EISDIR``;
    a blanket ``except OSError: return None`` turns either into the absent answer. Absent is not
    neutral here: on the ``try_clear_tag`` path it reads as "nothing saved", which is exactly the
    forged-empty result that lets ``wizard._clear_prior_pointer`` provision where it was supposed
    to abort. So one alias shape would be closed by the ``fstat`` below while others leaked
    through an error branch, in the same check-to-consume window this whole function is about. A
    read this path could not perform is a read it cannot judge, and on a path that chooses a
    ``destroy`` target that must refuse rather than resolve to a tag from somewhere else. The
    unpinned caller keeps degrading quietly, because its irreversible work is already done and it
    has nothing left to protect.

    Content stays TOLERANT, and that is not the same question: a document that is not a record
    means "there is no record here", which is a truthful answer about a file that was read
    successfully, and the legacy fallback it enables carries its own alias refusal.
    """
    try:
        fd = os.open(p, os.O_RDONLY | (_O_NOFOLLOW if pin else 0) | _O_BINARY)
    except FileNotFoundError:
        # The one failure that is a truthful answer rather than an unjudgeable read.
        return None
    except OSError as exc:
        _refuse_or_absent(p, exc, pin=pin)
        return None
    try:
        raw = _read_from(fd)
        if pin:
            # Raises SandboxCeilingUnsealable, a RuntimeError, so it is NOT caught by the
            # OSError handler below: an aliased record must refuse the command, never degrade
            # to "no record" and fall through to the legacy fields.
            require_unaliased_launch_state(str(p), fd=fd)
    except OSError as exc:
        _refuse_or_absent(p, exc, pin=pin)
        return None
    finally:
        os.close(fd)
    if len(raw) > _MAX_FILE_BYTES:
        logger.warning("launch state: %s is larger than %d bytes; ignoring it", p, _MAX_FILE_BYTES)
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        logger.warning("launch state: %s is not a readable JSON document; ignoring it", p)
        return None
    return data if isinstance(data, dict) else None


def _read_from(fd: int) -> bytes:
    """The first ``_MAX_FILE_BYTES + 1`` bytes of *fd*, looping over short reads.

    ``os.read`` may return fewer bytes than asked for without any error, so one call cannot
    be read as "this is the whole file": a short read would make an oversized document look
    like it fits the ceiling, and could truncate a legitimate record into unparseable JSON.
    Loops until the budget is met or the file ends.
    """
    budget = _MAX_FILE_BYTES + 1
    chunks: list[bytes] = []
    got = 0
    while got < budget:
        chunk = os.read(fd, budget - got)
        if not chunk:
            break
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)
