"""Seal the crew-home ceilings, refusing a spawn whose protected leaf is aliased.

Publishes empty absent ceilings, materialises their parent dirs, and refuses a launch
when a READONLY or HIDDEN leaf is a symlink, a junction, a dangling link or otherwise
not the real file the seal must pin. One of the owners ``kiro_crew.sandbox`` is carved
into: every name here stays readable and patchable as ``kiro_crew.sandbox.<name>``.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import shlex
import stat
import tempfile

from kiro_crew import platform_compat
from kiro_crew.config.paths import config_dir, kiro_agents_dir
from kiro_crew.sandbox_precreate import (
    _CEILING_TEMP_PREFIX,
    _CREW_NOFOLLOW_READONLY_DIR_LEAVES,
    _CREW_NOFOLLOW_READONLY_FILE_LEAVES,
    _CREW_PRECREATE_HIDDEN_DIR_LEAVES,
    _CREW_PRECREATE_READONLY_DIR_LEAVES,
    _CREW_PRECREATE_READONLY_FILE_LEAVES,
    _EMPTY_CEILING_DOCUMENT,
)
from kiro_crew.terminal_safe import safe_terminal_line

# Logged under the sandbox's name, which operator log filters, level settings
# and the tests' caplog scopes key on.
logger = logging.getLogger("kiro_crew.sandbox")


def _sealable_absent_ceilings() -> tuple[list[str], list[str]]:
    """Resolved (dir, file) paths that may be created so their disposition can apply.

    Two kinds of directory, one requirement. A read-only ceiling is materialised so
    the SEAL can apply; a hidden leaf is materialised so the MASK can. Both are
    skipped by their launcher loop when absent, so both need the path to exist
    before the loop runs, and the creation rules are identical -- 0o700, never
    truncate, never remove, refuse a dangling symlink.

    Resolved through ``config_dir()`` — the LIVE data home — rather than expanded over
    both ``_CREW_HOME_PREFIXES`` the way the deny lists are. A deny rule covers both
    spellings because either tree may still hold bytes; creation has the opposite
    requirement, since a stub in the deprecated ``~/.kirocrew`` of a migrated host is a
    file nothing will ever read. Whichever spelling ``config_dir()`` resolves to is
    already in the launcher's ``readonly_dirs``: both ``$HOME``-relative prefixes are
    listed there, and ``_relocated_crew_targets`` adds a data home that escapes
    ``$HOME``.

    Never raises: an unresolvable data home yields nothing and the seal behaves exactly
    as it did before this function existed. Neither does an ABSENT one -- a data home
    that is not a directory returns no targets, because a host with no install at all is
    deliberately not scaffolded here. The missing-parent refusal lives in
    :func:`_materialize_sealable_ceilings`, and only for a leaf whose OWN parent tree is
    present. Each ceiling is judged against the home that owns it: the crew-home leaves
    against ``config_dir()``, the kiro agents leaf against kiro-cli's own home.
    """
    try:
        root = str(config_dir())
    except Exception:  # pragma: no cover - defensive; a spawn must not fail on this
        logger.debug("could not resolve the crew data home for ceiling sealing", exc_info=True)
        return ([], [])
    if not os.path.isdir(root):
        # An ABSENT data home is the documented skip, not a failure: there is no
        # install to protect, and a host with no install at all is deliberately
        # not scaffolded here (the absent-data-home contract). Creating a tree
        # for it would fabricate governance state nobody asked for, so this
        # returns nothing and the seal behaves exactly as it did before this
        # function existed. The refusal below is for the opposite case -- a home
        # that IS present with a ceiling leaf whose parent is missing, which is a
        # genuine hole rather than an absent install.
        return ([], [])
    file_targets = [os.path.join(root, leaf) for leaf in _CREW_PRECREATE_READONLY_FILE_LEAVES]
    # Read-only ceilings AND hidden leaves: the seal needs the former to exist,
    # the mask needs the latter (see the docstring), so both are materialised here.
    dir_targets = [
        os.path.join(root, leaf)
        for leaf in _CREW_PRECREATE_READONLY_DIR_LEAVES + _CREW_PRECREATE_HIDDEN_DIR_LEAVES
    ]
    try:
        # The kiro agents tree (fork governance's specs + their lock; see the
        # readonly-target entry above): the Linux mount seal needs a directory
        # to bind, and an install may not have created it yet — an unsealed
        # absent dir would be creatable from inside the sandbox, placing
        # forged specs where the next spawn resolves them.
        #
        # Gated on ITS OWN parent rather than on the Kiro Crew data home `root`
        # above: this leaf hangs off kiro-cli's home (``KIRO_HOME``, else
        # ``~/.kiro``), a different resolver with a different lifecycle, and
        # ``config_dir()`` mkdirs whatever it returns — so testing `root` here
        # was true unconditionally and said nothing about this leaf. Read as
        # "no kiro install" while the refusal in
        # :func:`_materialize_sealable_ceilings` is about a PRESENT home whose
        # intermediate is missing, it turned every host without a kiro install
        # into a failed spawn. An absent ``~/.kiro`` is the documented skip, and
        # ``~/.kiro/agents/*.json`` is out of scope for this ceiling anyway (see
        # ``docs/system-specs/modules/governance.md``); a PRESENT one still
        # refuses below if its own intermediate is missing.
        agents_dir = str(kiro_agents_dir())
        if os.path.isdir(os.path.dirname(agents_dir)):
            dir_targets.append(agents_dir)
    except Exception:  # pragma: no cover - defensive, same posture as above
        logger.debug("could not resolve the kiro agents dir for sealing", exc_info=True)
    return (dir_targets, file_targets)


class SandboxCeilingUnsealable(RuntimeError):
    """A governance ceiling could not be made sealable, so the sandbox refuses to launch.

    The seal exists because an unsealed ceiling is a self-elevation hole: a sandboxed
    process that can write ``computer_use.json`` turns on desktop control for itself.
    Launching anyway would run the agent with that hole open while every log line said
    the ceiling was protected, so this is the ``_mount_or_die`` case rather than the
    best-effort one — a control was requested and could not be established.

    Raised out of ``namespace_argv``, so it surfaces to whichever ``wrap_argv`` caller
    asked for the spawn. Those callers report a failed operation; none of them falls back
    to running the command unconfined, which is what makes refusing safe here.

    The three states that reach it are all actionable by an operator, and the message
    names the path for that reason: a DANGLING SYMLINK squatting a ceiling path (either
    tampering, or a link whose destination went away); a data home where creation
    itself fails (a read-only mount, or a filesystem with no hardlink support); and a
    ceiling leaf whose parent directory is missing under a home that IS present,
    which cannot be created or published and would stay writable inside the sandbox.
    An ABSENT home is not among them -- that is the documented skip in
    :func:`_sealable_absent_ceilings`, not a refusal, and it is decided per ceiling
    against the home that owns that ceiling.
    """


# Named by the path callers import it from: an error chain renders each raised class
# as ``module.qualname`` (``subagent._describe_exception``), and that text reaches the
# Subagents panel and tracebacks, so this module's name must not leak into it.
SandboxCeilingUnsealable.__module__ = "kiro_crew.sandbox"


def _warn_unsealed_ceiling(target: str, exc: "OSError | None") -> None:
    """Say WHY the spawn is being refused: this ceiling could not be made sealable.

    Called immediately before :class:`SandboxCeilingUnsealable` is raised, so the spawn
    does NOT proceed — the log line carries the path and the errno that the exception
    message alone would not, and an operator reading it is the only one who can fix the
    data home. ``warning`` rather than ``debug`` for that reason: a refused spawn with no
    explanation is indistinguishable from an unrelated failure.

    Per spawn rather than once per process, matching the launcher's own ``expose_files``
    pre-read warning — a host where this keeps happening has a real problem, and
    de-duplicating it would hide how often the control cannot be established.
    """
    logger.warning(
        "sandbox: REFUSING to launch — could not create the governance ceiling %s (%s). "
        "mount(2) cannot seal a path that does not exist, so proceeding would leave it "
        "writable inside the sandbox",
        target,
        exc if exc is not None else "publish failed",
    )


#: Protected leaves where an ALIASED name is a hard spawn failure, not a warning.
#:
#: These two are reached by the SEALING loop, where ``_warn_if_alias_backed`` warns
#: deliberately: a sealed ceiling is an operator's config file and a dotfile manager
#: (chezmoi, stow) legitimately symlinks one, so refusing would turn a normal setup into a
#: spawn failure for a hole that is pre-existing and narrower than the breakage. The
#: MASKED leaves are a different population and refuse through
#: :func:`_refuse_aliased_masked_leaves`; this set is what the sealing loop refuses.
#:
#: These three are not config files and nothing has a reason to link them:
#:
#: * ``crew-panels`` -- created on demand by the GATEWAY and read by nothing else.
#:   It is bind-MASKED, so a link means the mask attaches to the target while the
#:   link name stays writable in the data home: a sandboxed process unlinks it,
#:   drops its own directory, and forges records the gateway reads back as
#:   authoritative -- past the ownership check and past the redactors.
#: * ``panel-templates`` -- holds the human-authored TEMPLATE whose separation from
#:   crew-published DATA is the whole containment story. Replacing that directory is
#:   authoring markup that renders in the panel, not changing a setting.
#: * ``panel-dismissals`` -- created on demand by the GATEWAY and read by nothing
#:   else, and the link hazard is identical to ``crew-panels``: the mask attaches to
#:   the target while the link NAME stays writable in the data home, so a sandboxed
#:   process unlinks it, drops its own directory, and forges dismissals the gateway
#:   reads back as the operator's -- hiding finished runs from the panel.
#: * ``member-records`` -- created by the GATEWAY and read by nothing else, with the
#:   same link hazard: the mask would attach to the target while the link NAME stays
#:   writable, and the gateway would then read rules, goals and DM bindings back from
#:   a directory a sandboxed process filled.
#:
#: So for these, a link is refused: the disposition must attach to the same name the
#: reader uses, and following a link is exactly the gap that voids it.
_CREW_NO_ALIAS_LEAVES: frozenset[str] = frozenset(
    {"crew-panels", "member-records", "panel-dismissals", "panel-templates"}
)


def _symlink_target_display(target: str) -> str:
    """Where the symlink *target* points, rendered for a refusal sentence.

    ``os.readlink`` hands back whatever the link holds, and every
    :class:`SandboxCeilingUnsealable` is printed to a terminal verbatim (``kirocrew
    chat``, ``kirocrew cloud``), so the value passes through ``safe_terminal_line`` here,
    where it is read, rather than at each refusal that quotes it.

    Two placeholders rather than an empty arrow: a link that cannot be READ renders as
    ``(unreadable)``, and one whose target is entirely control bytes escapes to the empty
    string, which would leave ``-> .`` and read as though the refusal had nothing to
    report. The party that plants the link chooses those bytes, so suppressing the value
    silently is the one outcome it must not buy; ``(unprintable)`` says the field was
    withheld and keeps the refusal itself intact.
    """
    pointed_at = "(unreadable)"
    with contextlib.suppress(OSError):
        pointed_at = os.readlink(target)
    return safe_terminal_line(pointed_at) or "(unprintable)"


def _refuse_if_aliased_protected_leaf(target: str) -> None:
    """Refuse the spawn when a protected leaf is reachable under a second name.

    Refuses a symlink at the name, and anything there that is not a real directory.
    Where ``_warn_if_alias_backed`` warns and continues for :data:`_CREW_NO_ALIAS_LEAVES`
    the outcome is a refusal: warning is what made this silent, because the log said the
    path was sealed while the writes went somewhere else. No ``st_nlink`` test, and not
    for lack of one -- every leaf in that set is a DIRECTORY, which cannot carry a second
    hard link, so the shape is unreachable rather than unchecked.
    """
    if os.path.basename(target.rstrip("/" + os.sep)) not in _CREW_NO_ALIAS_LEAVES:
        return
    try:
        info = os.lstat(target)
    except OSError:
        return
    if stat.S_ISLNK(info.st_mode):
        raise SandboxCeilingUnsealable(
            f"the protected directory {safe_terminal_line(target)} is a SYMLINK -> "
            f"{_symlink_target_display(target)}. Its "
            "disposition attaches to this NAME, so the link would leave the name "
            "replaceable inside the sandbox while reads and writes went to an "
            "unfenced inode. Remove the link and use a real directory."
        )
    if stat.S_ISDIR(info.st_mode):
        return
    raise SandboxCeilingUnsealable(
        f"the protected directory {safe_terminal_line(target)} is not a directory. It "
        "must be a real directory under this name for its mask to apply."
    )


def _warn_unsealed_ceiling_parent_missing(target: str, verb: str) -> None:
    """Say WHY the spawn is being refused when the ceiling's PARENT is missing.

    Its own message rather than :func:`_warn_unsealed_ceiling` with no errno,
    because nothing was created or published in this case -- reporting
    "publish failed" would name a syscall that was never attempted and send an
    operator looking at a write path that was never the problem. The leaf's own
    home is present here (an absent one is the documented skip, decided per
    ceiling in :func:`_sealable_absent_ceilings`), so this is a real hole: the
    leaf's parent is missing, so the leaf cannot be sealed and would stay
    writable from inside the sandbox.
    """
    logger.warning(
        "sandbox: REFUSING to launch — cannot %s the governance ceiling %s: its "
        "parent is not a directory. mount(2) cannot seal a path that does not "
        "exist, so proceeding would leave it writable inside the sandbox",
        verb,
        target,
    )


def _warn_if_alias_backed(target: str) -> None:
    """Warn when an ALREADY-PRESENT ceiling is reachable under a second name.

    ``MS_RDONLY`` binds a MOUNT, not an inode, so the seal only covers the path it was
    established on. Two shapes therefore survive it, and both are invisible to the seal
    loop because the path resolves and reads as present:

    * the ceiling is a **symlink**. The launcher seals the inode it resolves to, but the
      link NAME lives in the writable data home, so a sandboxed process can unlink it and
      put a real file of its own there instead;
    * the ceiling is a **regular file with another hardlink**. The alias is a different
      path, so it is outside the read-only mount, and a write through it changes the very
      inode the ceiling exposes.

    Reported, not refused, and deliberately so. Refusing would break the ordinary reasons
    a config file has a second name — a dotfile manager such as chezmoi or GNU stow, or a
    snapshot tool holding a hardlink — by turning them into a hard spawn failure, which is
    a much wider blast radius than the exposure. Neither shape is introduced here either:
    the ceilings this module publishes end at ``st_nlink == 1`` and are never symlinks, so
    this is a PRE-EXISTING property of every entry in ``readonly_dirs``, reachable only on
    a host where something else already created the ceiling that way. Closing it needs the
    data-home root sealed, which is a different change.

    The warning exists because the alternative is worse than the hole: without it the log
    says the ceiling is sealed while it is writable under another name.
    """
    try:
        info = os.lstat(target)
    except OSError:
        return
    if stat.S_ISLNK(info.st_mode):
        logger.warning(
            "sandbox: the governance ceiling %s is a SYMLINK. The seal covers the file it "
            "resolves to, but the link itself sits in a writable directory, so a sandboxed "
            "process can replace the name. Make it a regular file to close that.",
            target,
        )
    elif stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
        logger.warning(
            "sandbox: the governance ceiling %s has %d hardlinks. The seal covers this "
            "path only, so a write through another name reaches the same inode. Remove the "
            "extra link to close that.",
            target,
            info.st_nlink,
        )


def _refuse_if_dangling_symlink(target: str) -> None:
    """Refuse the spawn when *target* is a symlink that resolves to nothing.

    A RESOLVING symlink is left to the caller. Most legacy ceilings report that
    alias and seal its referent; a strict directory such as the gateway launcher
    follows this check with :func:`_refuse_if_symlink_leaf` because its lexical name
    selects executable code and must remain the mounted name.
    """
    if not os.path.islink(target) or os.path.exists(target):
        return
    raise SandboxCeilingUnsealable(
        f"the governance ceiling {safe_terminal_line(target)} is a DANGLING symlink -> "
        f"{_symlink_target_display(target)}. "
        "mount(2) cannot seal it and it would leave the path writable inside the "
        "sandbox. Remove or repoint it, or lower agent.sandbox to run without the seal "
        "deliberately."
    )


def _refuse_if_symlink_leaf(target: str) -> None:
    """Refuse when a masked or strict read-only directory leaf is a link.

    Stronger than :func:`_refuse_if_dangling_symlink`, which refuses only a link that
    resolves to nothing. A leaf that RESOLVES -- a link pointing at a real directory --
    is the attacker's entry here: ``os.path.isdir`` follows it and reports a directory,
    so the mask or seal binds over the link's TARGET, not the leaf name. The leaf name
    lives in the writable data home, so a sandboxed process can unlink it and drop an
    agent-controlled directory in its place. Refused, not removed, because ``lstat``
    then ``unlink`` is not atomic.
    """
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SandboxCeilingUnsealable(
            f"cannot stat the masked directory {safe_terminal_line(target)} to check for "
            f"a symlink: {safe_terminal_line(str(exc))}"
        ) from exc
    if stat.S_ISLNK(info.st_mode):
        raise SandboxCeilingUnsealable(
            f"the masked directory {safe_terminal_line(target)} is a SYMLINK -> "
            f"{_symlink_target_display(target)}. The mask would "
            "bind over the link's target, not the name, leaving the leaf replaceable in a "
            "writable parent so a sandboxed process could point the pre-created staging "
            "directory at a tree it controls. Remove or repoint it."
        )


def _is_link_or_junction(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or platform_compat.lstat_is_name_surrogate(info)


def _refuse_linked_models_leaf() -> None:
    """Refuse a sealed spawn when the live ``models`` NAME is a symlink or junction.

    The Seatbelt builder's half of the strict-name rule the namespace builder applies
    to every entry of :data:`_CREW_NOFOLLOW_READONLY_DIR_LEAVES` in
    :func:`_materialize_sealable_ceilings`; Seatbelt has no such pass, so it asks here.
    The name lives in the writable data home, so a deny rule written for it covers the
    link and leaves the tree it points at open, and a sandboxed process can write the
    weights there. Steps BELOW the name are not checked here: they sit inside the seal,
    so only the host can have linked one, and the loader that reads the file judges them
    (:func:`require_real_model_path`), failing that one load rather than every spawn.
    Metadata only; the link is neither followed nor removed.
    """
    target = os.path.join(str(config_dir()), "models")
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SandboxCeilingUnsealable(
            f"cannot stat the sealed model directory {safe_terminal_line(target)} to check "
            f"for a symlink: {safe_terminal_line(str(exc))}"
        ) from exc
    if _is_link_or_junction(info):
        raise SandboxCeilingUnsealable(
            f"the sealed model directory {safe_terminal_line(target)} is a SYMLINK or "
            f"JUNCTION -> {_symlink_target_display(target)}. The seal would cover the link "
            "and leave the tree it points at writable from the sandbox. Make models a real "
            "directory; to put the weights on another disk, relocate the whole data home "
            "(KIROCREW_HOME), so the seal covers them."
        )


def require_real_model_path(path: str, *, models_root: str) -> None:
    """Refuse a model LOAD when any name from *models_root* down to *path* is a link.

    PUBLIC, and called by each loader that reads weights from the sealed ``models``
    tree before it opens the file: whisper's ``stt.models.ModelStore._accept_existing``
    (so a link refuses the verify and a download through it alike) and the GGUF's
    ``embeddings._model_file_stamp_unaliased``. The decoder needs no call: the bytes it
    runs are a snapshot ``transcribe._authenticated_ffmpeg`` re-hashes after copying.

    Both backend seals cover paths, not inodes, so a symlink or junction anywhere on the
    way leaves the bytes the loader reads at a target the seal may not cover. A step
    below ``models`` sits inside the seal, so only the host can have made such a link,
    and it threatens only this file's bytes; refusing here fails this one load, where a
    check on the spawn path would refuse every sealed spawn on the host. The ``models``
    name itself is walked too, and also refuses the spawn
    (:func:`_refuse_linked_models_leaf`), because that name sits in the writable data
    home. A HARD LINK on the file is judged on the loader's descriptor instead
    (:func:`require_unaliased_model_file`), and the loader's ``O_NOFOLLOW`` open
    refuses a leaf that becomes a link after this walk.

    Metadata only: ``lstat`` per name, nothing followed, read or removed. A missing
    name ends the walk (the loader then finds no file, or downloads one); a name that
    cannot be stat'ed refuses this load. A path outside *models_root* is not the
    seal's and is not checked.
    """
    root = os.path.normpath(models_root)
    try:
        relative = os.path.relpath(os.path.normpath(path), root)
    except ValueError:  # different Windows drives: not under the models tree
        return
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        return
    steps = [] if relative == os.curdir else relative.split(os.sep)
    targets = [root]
    for step in steps:
        targets.append(os.path.join(targets[-1], step))
    for target in targets:
        try:
            info = os.lstat(target)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise SandboxCeilingUnsealable(
                f"cannot stat the sealed model path {safe_terminal_line(target)} to check "
                f"for a link: {safe_terminal_line(str(exc))}. This model is not loaded."
            ) from exc
        if _is_link_or_junction(info):
            raise SandboxCeilingUnsealable(
                f"the sealed model path {safe_terminal_line(target)} is a SYMLINK or "
                f"JUNCTION -> {_symlink_target_display(target)}. The seal covers paths, so "
                "its target could remain writable outside the model tree, and this model "
                "is not loaded. Replace the link with a real directory or file under "
                "models/; to put the weights on another disk, relocate the whole data home "
                "(KIROCREW_HOME), so the seal covers them."
            )


def _require_real_dir_nofollow(target: str) -> None:
    """Confirm *target* is a real directory with NO-FOLLOW semantics, else refuse.

    Called after ``mkdir`` loses the ``EEXIST`` race: something else won the create, and
    ``FileExistsError`` alone does not say WHAT now sits at the name. A plain
    ``os.path.isdir`` would follow a symlink that a racing sandboxed process planted in
    the window between our checks, so the mask would bind over that link's target. Use
    ``lstat`` (never follows) and require a real directory at the leaf itself; a link, a
    file, or anything else there refuses the spawn rather than mask an attacker's target.
    """
    try:
        info = os.lstat(target)
    except OSError as exc:
        raise SandboxCeilingUnsealable(
            f"cannot re-check the masked directory {safe_terminal_line(target)} after a "
            f"create race: {safe_terminal_line(str(exc))}"
        ) from exc
    if stat.S_ISLNK(info.st_mode):
        raise SandboxCeilingUnsealable(
            f"the masked directory {safe_terminal_line(target)} became a SYMLINK -> "
            f"{_symlink_target_display(target)} in the create "
            "race. Refusing rather than binding the mask over the link's target."
        )
    if not stat.S_ISDIR(info.st_mode):
        raise SandboxCeilingUnsealable(
            f"cannot mask {safe_terminal_line(target)}: a non-directory won the create "
            "race at the path"
        )


def _require_real_file_nofollow(
    target: str,
    *,
    harm: str,
    remedy: str,
    fd: "int | None" = None,
    subject: str = "strict governance ceiling",
) -> None:
    """Confirm *target* is a lone regular file, else refuse. For strict file leaves only.

    The file analogue of :func:`_require_real_dir_nofollow`, and stricter than
    :func:`_warn_if_alias_backed` on purpose. That function only WARNS about the two
    shapes an ``MS_RDONLY`` bind cannot cover -- a symlink whose NAME stays replaceable,
    and a regular file carrying a second hardlink whose alias sits outside the mount --
    because refusing them for every ceiling would turn an ordinary dotfile manager or
    snapshot tool into a hard spawn failure, a wider blast radius than the exposure.

    That trade is right for a ceiling whose worst case is a stale policy, and wrong for a
    leaf whose contents pick an unrecoverable action. Two do: an aliased ``cloud.json``
    picks the container image a Fargate launch runs, and the task's execution role
    delivers the model credential into it; an aliased launch record picks which stack a
    ``cloud destroy --yes`` deletes. Both refuse, and both refuse at the seam that
    CONSUMES the file rather than on the spawn path, so the cost is one command's refusal
    instead of every sandboxed spawn on a host whose files legitimately carry a second
    name. Callers keep using :func:`_warn_if_alias_backed` for every other leaf.

    *harm* names what a write through the second name buys, and *remedy* is the one
    command that clears it. They come from the CALLER, with no default, because the two
    strict leaves are refused for different reasons and a message naming the wrong
    consequence sends an operator at the wrong file -- and because a third leaf must state
    its own reason rather than silently inherit the first one's.

    UNCONDITIONAL, and an earlier version of this was not. It exempted a file that
    carried no Fargate block, on the reasoning that an alias selects no image when there
    is no image -- which is wrong, because the agent does not need to swap a field it can
    CREATE. Given a writable alias and no block, an agent writes a COMPLETE block through
    the alias and the owner's next launch runs the image it chose. That is the same
    reasoning that already put this leaf in the pre-create list, where an ABSENT file is
    the dangerous case for exactly this reason; the exemption failed to carry it one step
    further. Any rule that reads the file's current contents has the same hole, because
    contents are what the attacker supplies, so this rule reads no contents at all.

    *fd* is an OPEN descriptor for the file the caller has ALREADY READ, and it changes
    which inode this answers about. Without it the check ``lstat``s the NAME and the
    caller then opens that name again, so the inode that was judged and the inode that was
    consumed are two separate resolutions and nothing ties them together. With it the
    judgement lands on the very descriptor the bytes came from, so a swap at the name
    between the two cannot put un-judged content in front of a caller: the alias question
    is asked about what was read rather than about what the name pointed at earlier.

    The symlink branch is skipped in that mode because it cannot arise there and cannot be
    answered there: a descriptor obtained with ``O_NOFOLLOW`` refuses a symlinked leaf at
    ``open`` time with ``ELOOP``, and ``fstat`` on an ordinary descriptor never reports
    ``S_IFLNK`` in any case. Callers therefore keep the by-name form as well, which is what
    still produces the symlink refusal and its remedy.

    *subject* names the KIND of file in the refusal, because the sentences are read by an
    operator who has to find the thing being described. It defaults to the governance
    ceilings this was written for; a caller guarding a file that is not a ceiling passes its
    own noun rather than sending that operator looking for a policy document.
    """
    if fd is not None:
        try:
            info = os.fstat(fd)
        except OSError as exc:
            raise SandboxCeilingUnsealable(
                f"cannot stat the {subject} {safe_terminal_line(target)}: "
                f"{safe_terminal_line(str(exc))}"
            ) from exc
    else:
        try:
            info = os.lstat(target)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise SandboxCeilingUnsealable(
                f"cannot stat the {subject} {safe_terminal_line(target)}: "
                f"{safe_terminal_line(str(exc))}"
            ) from exc
        if stat.S_ISLNK(info.st_mode):
            raise SandboxCeilingUnsealable(
                f"the {subject} {safe_terminal_line(target)} is a SYMLINK -> "
                f"{_symlink_target_display(target)}. The seal "
                "binds the file it resolves to while the link name stays in a writable "
                "directory, so a sandboxed process could replace the name and "
                f"{safe_terminal_line(harm)}. {safe_terminal_line(remedy)}"
            )
    if not stat.S_ISREG(info.st_mode):
        raise SandboxCeilingUnsealable(
            f"the {subject} {safe_terminal_line(target)} is not a regular file, so the "
            "read-only bind would not cover what a reader resolves there. "
            f"{safe_terminal_line(remedy)}"
        )
    if info.st_nlink > 1:
        raise SandboxCeilingUnsealable(
            f"the {subject} {safe_terminal_line(target)} has "
            f"{info.st_nlink} hardlinks. A bind mount seals a MOUNT, not an inode, so a "
            "write through the other name reaches the very inode this seal exposes "
            f"and can {safe_terminal_line(harm)}. {safe_terminal_line(remedy)}"
        )


def _publish_empty_ceiling(
    target: str, parent: str, content: bytes = _EMPTY_CEILING_DOCUMENT
) -> bool:
    """Write *content* (default: the empty document) to a sibling temp, then link it in.

    Two steps rather than ``open(target, O_CREAT | O_EXCL)`` followed by a write,
    because the one-step form publishes the NAME before the BYTES: a crash, a full
    disk, or a signal in between leaves a zero-length file at the ceiling path, and
    zero length is not valid JSON — the reader would see corrupt where this function
    means absent. Here the target only ever appears once its content is complete.

    ``os.link`` is the publish because it is the no-clobber one: unlike ``os.replace``
    it fails with ``EEXIST`` instead of overwriting, so a racing spawn — or an operator
    writing the real document in the same instant — keeps its file. That is also why
    the ``os.path.exists`` pre-check upstream is an optimisation and not the guard.

    ``mkstemp`` creates the temp file 0o600 before the first byte, so no separate
    lockdown call is needed (and none may be added: a lockdown applied after content
    reaches a published path is the defect ``scripts/check_lockdown_before_publish.py``
    refuses). The mode needs no reassertion either — a umask can only clear bits, never
    add them.

    Returns ``False`` on any failure, having published nothing. The caller decides what
    a failure means; this function's only contract is that the ceiling path is either
    absent or holds the complete document.
    """
    fd = -1
    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(dir=parent, prefix=_CEILING_TEMP_PREFIX, suffix=".tmp")
        # ``os.write`` is not obliged to consume the whole buffer, and a short write is
        # not an error — it returns a count. Taking that count for success would link a
        # TRUNCATED document, which reads as corrupt rather than as absent and is the
        # exact outcome the temp-then-link shape exists to prevent. Loop, and treat zero
        # progress as an error so a filesystem that accepts nothing cannot spin here.
        view = memoryview(content)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError(errno.EIO, "short write to a ceiling temp file", tmp)
            view = view[written:]
        os.close(fd)
        fd = -1
        os.link(tmp, target)
        return True
    except OSError:
        return False
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        if tmp:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def _materialize_sealable_parent_dirs(target: str, leaf: str) -> None:
    """Create a nested ceiling's parents without following aliases.

    ``leaf`` is the full relative entry from the strict no-follow list. Each
    intermediate component gets the same owner-only mode and post-race
    validation as the final directory. The data-home root itself is never
    created here.
    """
    relative = os.path.normpath(leaf)
    suffix = os.sep + relative
    normalized = os.path.normpath(target)
    if not normalized.endswith(suffix):
        raise SandboxCeilingUnsealable(
            f"cannot derive the data home for nested governance ceiling {target}"
        )
    data_home = normalized[: -len(suffix)]
    parent_relative = os.path.dirname(relative)
    if not parent_relative or parent_relative == "." or not os.path.isdir(data_home):
        return

    current = data_home
    for component in parent_relative.split(os.sep):
        current = os.path.join(current, component)
        _refuse_if_dangling_symlink(current)
        _refuse_if_symlink_leaf(current)
        if os.path.exists(current):
            _require_real_dir_nofollow(current)
            continue
        try:
            os.mkdir(current, 0o700)
        except FileExistsError:
            _require_real_dir_nofollow(current)
        except OSError as exc:
            raise SandboxCeilingUnsealable(
                f"cannot create the governance ceiling parent {current}: {exc}"
            ) from exc


def _note_established(established: list[str] | None, target: str) -> None:
    """Record *target* as seen present by THIS process, for the launcher to require.

    Separate from each materialiser's return value, which means "what I created" and
    is asserted as such by callers that check an existing ceiling was left alone. The
    launcher needs a different set: every protected target this pass has just SEEN,
    created or already there. It is collected HERE, where the pass is already
    statting and creating off the event loop, and never in the plan --
    ``test_the_planner_reads_nothing_but_its_arguments`` pins that the planner reads
    no path at all, because on a stalled home each probe would block the one loop
    every session, cron and heartbeat shares.
    """
    if established is not None:
        established.append(target)


def _mint_gateway_id_before_seal() -> None:
    """Mint the real gateway id so the read-only seal has the gateway's own inode to bind.

    The id is minted lazily on first read and never changes after that, and
    ``mount(2)`` cannot seal an absent name. Minting here, before the launcher builds
    its mounts, gives the ``gateway_id`` READONLY leaf a target on a fresh install.
    A pre-created stub would not do: the gateway's later mint would ``os.replace`` the
    bound name from the host namespace, which detaches the bind in a running sandbox.

    Skipped for an absent data home, which this path deliberately never scaffolds.
    Imported lazily so this module keeps not loading the config loader. Never raises:
    ``gateway_id`` already falls back to a process-local id on any failure.
    """
    try:
        if not os.path.isdir(config_dir()):
            return
        from kiro_crew import gateway_identity

        gateway_identity.gateway_id()
    except Exception:  # pragma: no cover - defensive; a spawn must not fail on this
        logger.debug("could not mint the gateway id before sealing", exc_info=True)


def _materialize_sealable_ceilings(established: list[str] | None = None) -> list[str]:
    """Create every absent sealable ceiling; return the paths actually created.

    Runs on the Linux spawn path only, immediately before the launcher builds its
    ``readonly_dirs`` mount sequence, so a ceiling that did not exist a moment ago is
    a read-only mountpoint by the time the sandboxed command runs.

    **Fail-closed.** If a ceiling cannot be made sealable this raises
    :class:`SandboxCeilingUnsealable` and the spawn does not happen. That is a
    deliberate reversal of an earlier best-effort version, which warned and continued:
    continuing means the launcher's ``os.path.exists`` guard skips the path, so the
    sandboxed process runs with a writable governance keystone and nothing downstream
    notices. An unsealed ceiling is the one thing this function exists to prevent, so it
    refuses for the same reason ``_mount_or_die`` refuses a failed hiding mount.

    Three states trigger it:

    * a **missing parent** for a ceiling path. Creating the parents would build
      a writable ancestor the agent could rename through, so the safe answer is
      to refuse rather than scaffold;
    * a **dangling symlink** squatting a ceiling path. ``os.path.exists`` follows
      symlinks, so it reads as absent to this function AND to the launcher's guard,
      while ``os.link`` refuses the name as ``EEXIST`` — the sandboxed process's write
      then follows the link and the host reads the result back through the ceiling path.
      It is not removed here: ``islink`` followed by ``unlink`` is not atomic, and the
      dashboard publishes a real keystone over that same name with ``atomic_write``, so
      a removal racing a validated operator write would delete the operator's new
      settings. POSIX offers no unlink-only-if-still-a-symlink, so the safe answer is to
      refuse and let a human resolve it;
    * a **creation failure** other than ``EEXIST`` — a read-only mount, or a filesystem
      with no hardlink support.

    ``EEXIST`` is benign for ordinary ceilings: another spawn or the operator won
    the race and the launcher seals the winner. Strict executable directories have
    a higher bar. Their winner is re-checked with ``lstat`` and must be a real
    directory, because a symlink would move the mount off the name the gateway later
    resolves.

    Never TRUNCATES and never REMOVES: an existing ceiling is left byte-for-byte alone,
    so this can only ever add the absent default.
    """
    created: list[str] = []
    dir_targets, file_targets = _sealable_absent_ceilings()
    _mint_gateway_id_before_seal()

    for target in dir_targets:
        normalized = os.path.normpath(target)
        strict_leaf = next(
            (
                leaf
                for leaf in _CREW_NOFOLLOW_READONLY_DIR_LEAVES
                if normalized.endswith(os.sep + os.path.normpath(leaf))
            ),
            None,
        )
        strict_nofollow = strict_leaf is not None
        if strict_leaf is not None:
            _materialize_sealable_parent_dirs(target, strict_leaf)
        _refuse_if_dangling_symlink(target)
        # BEFORE the warn-and-continue below: for a protected leaf an alias is a
        # refusal, and reaching `_warn_if_alias_backed` would log that the path was
        # covered while the bytes went elsewhere.
        _refuse_if_aliased_protected_leaf(target)
        if strict_nofollow:
            _refuse_if_symlink_leaf(target)
        if os.path.exists(target):
            _note_established(established, target)
            if strict_nofollow:
                _require_real_dir_nofollow(target)
            else:
                # Present, so the launcher will seal it -- but say so when the seal is
                # reachable around rather than through this path.
                _warn_if_alias_backed(target)
            continue
        if not os.path.isdir(os.path.dirname(target)):
            _warn_unsealed_ceiling_parent_missing(target, "create")
            raise SandboxCeilingUnsealable(
                f"cannot seal the governance ceiling {target}: its parent is not "
                "a directory; it would stay writable inside the sandbox"
            )
        try:
            # 0o700 needs no reassertion: a umask can only clear bits, never add them.
            os.mkdir(target, 0o700)
        except FileExistsError:
            if strict_nofollow:
                # A competing creator may have planted a link after the check above.
                # Re-check the winner without following it before trusting the name.
                _require_real_dir_nofollow(target)
            _note_established(established, target)
            continue
        except OSError as exc:
            _warn_unsealed_ceiling(target, exc)
            raise SandboxCeilingUnsealable(
                f"cannot create the governance ceiling {target}: {exc}"
            ) from exc
        created.append(target)
        _note_established(established, target)

    for target in file_targets:
        parent = os.path.dirname(target)
        _refuse_if_dangling_symlink(target)
        if os.path.exists(target):
            _note_established(established, target)
            # WARNS for every leaf, strict ones included. This function runs from
            # ``namespace_argv`` on every Linux sandboxed spawn, so refusing here refuses the
            # whole host's agent work -- a chat turn, a cron job, a subagent -- whenever a
            # governance leaf carries a second name, and a second name is what stow, chezmoi
            # and ``rsync --link-dest`` leave behind. That cost is not paid for the residual:
            # each strict leaf's real harm is answered at the seam that CONSUMES the file --
            # ``require_unaliased_cloud_config`` from the provisioner seam for ``cloud.json``,
            # whose alias would choose the container a launch hands the model credential to,
            # and ``require_unaliased_launch_state`` from ``LaunchState.load`` for the launch
            # record, whose alias would choose the stack a ``destroy --yes`` deletes.
            # The seal is defence in depth here, not the property the design rests on.
            #
            # The warn still fires, because the alias really is outside the read-only bind:
            # a silent skip would leave the log claiming a seal that another name reaches.
            _warn_if_alias_backed(target)
            continue
        if not os.path.isdir(parent):
            _warn_unsealed_ceiling_parent_missing(target, "publish")
            raise SandboxCeilingUnsealable(
                f"cannot publish the governance ceiling {target}: its parent is "
                "not a directory; it would stay writable inside the sandbox"
            )
        if _publish_empty_ceiling(target, parent):
            created.append(target)
            _note_established(established, target)
        elif not os.path.exists(target):
            # Absent after a failed publish, so nothing won the race: the seal really
            # did not apply. ``exists`` rather than a plumbed-through errno because the
            # publish is two syscalls and only the OUTCOME decides whether this matters.
            _warn_unsealed_ceiling(target, None)
            raise SandboxCeilingUnsealable(
                f"cannot publish the governance ceiling {target}; it would stay writable "
                "inside the sandbox"
            )
        else:
            # A competing creator won the publish. Re-checked on the same terms as the
            # present-file branch above -- warned, not refused -- because the reason is the
            # same: this path decides whether every spawn on the host runs, and the alias
            # harm is answered where a launch consumes the file.
            _warn_if_alias_backed(target)
            # Accepted, so ESTABLISHED: losing the race still ends with the object there
            # and this pass having just seen it, which is the same standing a target this
            # pass created has. Leaving it out would hand the launcher a set missing
            # exactly the names a concurrent creator touched.
            _note_established(established, target)

    return created


def _warn_aliased_strict_leaves() -> None:
    """Report an aliased strict leaf on the universal spawn path, without refusing.

    Same two shapes :func:`_warn_if_alias_backed` covers, over the strict leaves, and
    reported for the same reason: a second name on a configuration file is what a dotfile
    manager or a hardlinking backup leaves behind, and refusing it here refuses EVERY
    sandboxed spawn on the host. The seal is still not covering that alias, so saying
    nothing would leave a log claiming a seal that is reachable under another name.

    The refusal for the one leaf whose alias picks a credential recipient lives at the point
    that consumes it, :func:`require_unaliased_cloud_config`, so the consequence lands on the
    launch rather than on everything else the host does.
    """
    for leaf in _CREW_NOFOLLOW_READONLY_FILE_LEAVES:
        _warn_if_alias_backed(os.path.join(str(config_dir()), leaf))


def require_unaliased_cloud_config() -> None:
    """Refuse an aliased ``cloud.json`` at the point a launch consumes it.

    PUBLIC, and called from ``platform.defaults.DefaultRemoteProvisionerProvider`` where the
    saved block becomes a `FargateLaunchEngine`. That is the only place the alias matters: a
    write through an unsealed second name picks the container image a launch runs, and the
    task's execution role delivers the model credential into it.

    Placed here rather than on the spawn path so the refusal costs one lane's launch instead
    of every sandboxed spawn on a host whose files legitimately carry a second name. It is
    still unconditional on CONTENT -- an agent that can write through an alias does not need
    to swap a block it can create -- and reads no file contents, only ``lstat``.

    Platform-independent, and that is why it is one function rather than a check inside each
    launcher: the refusal is a fact about the NAME and never depended on the sealing
    mechanism, so a per-launcher copy would only give the two platforms something to drift
    on. Both mechanisms are name-based and neither follows a link -- a read-only bind seals a
    mount, a Seatbelt ``deny file-write*`` matches a pathname -- so in each case the alias
    reaches the same inode by a name the rule does not cover.
    """
    for leaf in _CREW_NOFOLLOW_READONLY_FILE_LEAVES:
        _require_real_file_nofollow(
            os.path.join(str(config_dir()), leaf),
            harm=(
                "choose the container image a Fargate launch runs, which is what the task's "
                "execution role delivers the model credential into"
            ),
            remedy=(
                "Make the path a lone regular file: replace a link with a regular file, or "
                "break the extra hardlink."
            ),
        )


def _delete_file_command(target: str, *, windows: "bool | None" = None) -> str:
    """The command an operator runs to delete *target*, in the shell they are actually in.

    A refusal's whole value is that the person reading it can act on it, so the remedy has to
    be a command that exists on their box. ``rm`` is not one on Windows outside PowerShell, and
    POSIX single quotes are not quoting characters to ``cmd`` at all -- so ``shlex.quote`` on a
    ``C:\\...`` path produces ``rm 'C:\\Users\\...'``, which names a command they do not have,
    quoted in a way their shell would not accept, for a file they do have.

    ``del`` is the one spelling that works in both shells a Windows operator is plausibly in:
    a ``cmd`` builtin, and a PowerShell alias for ``Remove-Item``. Double quotes are what both
    accept, and a Windows path cannot contain ``"``, so no escaping question arises.

    *windows* defaults to this host and exists as a PARAMETER so BOTH renderings are
    exercisable from one platform. Every platform defect in this area came from the same shape:
    a string written once, verified where it was written, and asserted as general. A default-only
    reading of ``IS_WINDOWS`` would leave the Windows branch measurable on Windows alone, which
    is how this one reached CI.
    """
    on_windows = platform_compat.IS_WINDOWS if windows is None else windows
    if on_windows:
        return f'del "{target}"'
    return f"rm {shlex.quote(target)}"


def require_unaliased_launch_state(path: str, *, fd: "int | None" = None) -> None:
    """Refuse an alias-backed launch record at the point a command consumes its tag.

    PUBLIC, and called from ``cloud.launch_state.LaunchState.load`` -- the one read every
    tag-consuming verb goes through, and the read ``cloud destroy`` resolves its target
    from. The harm is narrower than ``cloud.json``'s and just as unrecoverable: a write
    through an unsealed second name puts any tag in the record, and ``cloud destroy --yes``
    deletes the stack it names. ``cloud launch`` re-attaching to a forged tag is the same
    substitution, quieter.

    The record is sealed against agent writes on three layers already -- the file-write
    gate, the kernel read-only seal, and pre-creation so an absent name cannot be squatted
    -- and every one of those covers a PATH. An alias reaches the same inode by a name none
    of them names, which is exactly the gap this refuses; the layers are what keep an agent
    from creating the alias in the first place, and this is what stops a tag being consumed
    from one that already exists.

    Takes the path being READ rather than deriving it, so the file this checks and the file
    the caller goes on to consume cannot be two different files.

    *fd* is how the check stops being a check-then-use. Without it this ``lstat``s the NAME
    and the caller opens that name again, so the judged inode and the consumed inode are two
    resolutions with a window between them. ``LaunchState.load`` therefore calls this twice:
    once by name, which is what refuses a symlinked leaf and names the remedy, and once on
    the DESCRIPTOR the record's bytes were actually read from, after the read. The second
    call is what ties the answer to the inode that was consumed, so content that was never
    judged cannot be put in front of a caller by swapping the name in between.

    What that does NOT close, stated because the guard's value depends on it: an alias that
    existed EARLIER, was written through in place, and was unlinked before this read leaves a
    lone regular file holding forged bytes, and no ``lstat`` or ``fstat`` rule can see that
    an inode once had a second name. Closing that shape needs the tag verified through a
    channel the sandbox cannot reach, or the unbounded verb requiring an explicit ``--tag``;
    both are design choices for the launch lane rather than something this seam can decide.
    So this remains one layer of several -- what it now guarantees is that the layer answers
    about the right inode.

    NOT added to :data:`_CREW_NOFOLLOW_READONLY_FILE_LEAVES`. That list is walked where a
    spawn is prepared, so a leaf in it refuses every sandboxed spawn on a host whose files
    legitimately carry a second name (stow, chezmoi, ``rsync --link-dest``) -- the whole box
    for one command's exposure, and the regression a review already blocked once. The warn
    on the spawn path stays a warn.
    """
    _require_real_file_nofollow(
        path,
        harm=(
            "choose which stack `kirocrew cloud destroy --yes` deletes, and a deleted stack "
            "and its data do not come back"
        ),
        remedy=(
            f"Remove the aliased name with `{_delete_file_command(path)}`; `kirocrew cloud list` "
            "finds your instance again."
        ),
        fd=fd,
    )


def require_unaliased_model_file(path: str, *, fd: int) -> None:
    """Refuse an alias-backed loader file at the point a loader is about to trust it.

    PUBLIC, and called from ``embeddings._model_file_stamp_unaliased`` before llama.cpp maps
    the bundled GGUF, and from ``stt.models.ModelStore._verified_on_disk`` -- the single gate
    for "there is already a usable file here", which hashes the file and then hands its PATH
    to a native loader that reopens it. That gap is the whole exposure: a second name on the
    same inode is a writable handle onto bytes the digest already blessed, and the ``models``
    seal covers paths, so no mount or Seatbelt rule names the alias. The GGUF has no
    digest at load, which makes the same alias worse rather than harmless: nothing else
    stands between a write through it and the next load.

    Takes a DESCRIPTOR and nothing else, which is what makes this a check rather than a
    check-then-use. The judgement lands on the inode the digest was computed from, so a swap
    at the name between the hash and this call cannot put un-judged content in front of the
    loader. There is deliberately no by-name mode: ``O_NOFOLLOW`` on the loader's own open
    already refuses a symlinked leaf, reported as this same exception, and the loader's own
    walk (:func:`require_real_model_path`) is what refuses a symlink or junction at the
    directory components above it.

    Refuses the LOAD, not the spawn. The blast radius of refusing here is one model load on a
    host whose weights carry a second name; refusing on the spawn path would cost every
    sandboxed spawn on that host, which is the whole box for one file's exposure.

    What this does NOT close: an alias that existed earlier, was written through, and was
    unlinked before the check leaves a lone regular file holding forged bytes, and no
    ``fstat`` rule can see that an inode once had a second name; the whisper digest catches
    those bytes, and the GGUF has no digest at load. Nor does it see a descriptor opened
    through such an alias and still held. The whisper verifier judges before its first read
    and refuses an inode stamp that moves during the read, but a write through a held
    descriptor after the hash and before the native loader reopens the path is not seen.
    The seal and the file-write gate are what keep the agent from creating the alias at
    all; this is what stops bytes being consumed through one that already exists.
    """
    _require_real_file_nofollow(
        path,
        harm=(
            "replace the verified weights with bytes no digest covered, which the loader "
            "then reopens by name and uses for every later request"
        ),
        remedy=(
            "Replace the link with a plain copy: copy it to a temporary name in the same "
            "directory, then rename that copy over this path."
        ),
        fd=fd,
        subject="verified model file",
    )
