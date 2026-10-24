"""Materialise the mask targets the launcher binds over crew-home leaves.

The plain masked directories, the live-target pointer, the md-notebook state and the
auth-store staging links: each target is created before the launcher binds over it, its
legacy temp files are swept, and an unfit live-target pointer is reported. A new mask
target goes here. One of the owners ``kiro_crew.sandbox`` is carved into: every name
here stays readable and patchable as ``kiro_crew.sandbox.<name>``.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shlex
import stat
from pathlib import Path
from typing import NamedTuple

from kiro_crew.atomic_write import fsync_dir
from kiro_crew.config.paths import config_dir
from kiro_crew.sandbox_aliases import _masked_crew_home_roots
from kiro_crew.sandbox_leaves import (
    _AUTH_STORE_STAGING_LEAF,
    _CREW_HOME_PREFIXES,
    _LIVE_TARGET_LEAF,
    _LIVE_TARGET_STAGING_LEAF,
    _MD_NOTEBOOK_STATE_FILES,
    MD_NOTEBOOK_APP_NAME,
)
from kiro_crew.sandbox_precreate import (
    _CEILING_TEMP_PREFIX,
    _CREW_PRECREATE_HIDDEN_DIR_LEAVES,
    _LIVE_TARGET_PRECREATE_CONTENT,
    _MD_NOTEBOOK_PRECREATE_CONTENT,
)
from kiro_crew.sandbox_seals import (
    SandboxCeilingUnsealable,
    _note_established,
    _publish_empty_ceiling,
    _refuse_if_dangling_symlink,
    _refuse_if_symlink_leaf,
    _require_real_dir_nofollow,
)
from kiro_crew.sandbox_tiers import carveout_chain_has_planted_link
from kiro_crew.terminal_safe import safe_terminal_line

# Logged under the sandbox's name, which operator log filters, level settings
# and the tests' caplog scopes key on.
logger = logging.getLogger("kiro_crew.sandbox")


def _materialize_maskable_dirs(established: list[str] | None = None) -> list[str]:
    """Create the absent on-demand HIDDEN directories so the mask loop can bind over them.

    The mirror image of :func:`_materialize_sealable_ceilings` for
    :data:`_CREW_PRECREATE_HIDDEN_DIR_LEAVES`: the launcher's ``sensitive_dirs`` loop
    is guarded on ``isdir``, so an absent target gets no empty bind over it and the
    directory the gateway creates later is visible to the running sandbox. Same
    fail-closed shape as the ceilings -- a dangling link squatting the name, a plain
    file where the directory should be, or a creation failure other than ``EEXIST``
    refuses the spawn, because launching anyway runs the agent with the directory
    unmasked. Plain ``mkdir``, deliberately: every leaf here is a direct child of
    the data home, and a leaf that needed an intermediate directory would have an
    agent-writable ancestor a rename could swap out from under the mask.

    Returns the paths it created, so a test can check each one reaches the launcher's
    hidden list.
    """
    created: list[str] = []
    try:
        root = str(config_dir())
    except Exception as exc:
        # Fail CLOSED, unlike a best-effort lookup: a spawn that went ahead
        # without the target would run the agent with the staging directory
        # unmasked, which is exactly the exposure this function exists to
        # prevent. A data home that cannot be resolved is a host problem to
        # surface, not a mask to skip.
        raise SandboxCeilingUnsealable(
            f"cannot resolve the crew data home to materialise the masked directories: {exc}"
        ) from exc
    for leaf in _CREW_PRECREATE_HIDDEN_DIR_LEAVES:
        target = os.path.join(root, leaf)
        _refuse_if_dangling_symlink(target)
        # A leaf that is itself a symlink/junction -- even one that RESOLVES to a real
        # directory -- is the attack entry: ``isdir`` would follow it and the mask would
        # bind over the target, not the replaceable name. Refuse before the isdir check.
        _refuse_if_symlink_leaf(target)
        if os.path.isdir(target):
            _note_established(established, target)
            continue
        if os.path.exists(target):
            raise SandboxCeilingUnsealable(
                f"cannot mask {target}: a non-directory is sitting at the path"
            )
        try:
            os.mkdir(target, 0o700)
        except FileExistsError:
            # Something won the create race. ``FileExistsError`` does not say what now
            # sits at the name, so re-validate with NO-FOLLOW semantics: a symlink an
            # attacker slipped in during the window must refuse, not be masked over.
            _require_real_dir_nofollow(target)
            _note_established(established, target)
            continue
        except OSError as exc:
            raise SandboxCeilingUnsealable(
                f"cannot create the masked directory {target}: {exc}"
            ) from exc
        created.append(target)
        _note_established(established, target)
    return created


def _materialize_live_target_mask_target(
    established: list[str] | None = None,
) -> str | None:
    """Publish the live-target pointer's absent-equivalent document so its mask can mount.

    The FILE counterpart of :func:`_materialize_maskable_dirs`, and it shares that
    function's simplifying property: the pointer is a DIRECT child of the data home, so
    there is no agent-writable intermediate component for a planted link to redirect, and
    no per-component descent is needed — the whole hazard
    :func:`_materialize_md_notebook_mask_targets` walks chains to avoid.

    Why at all: the launcher's ``sensitive_files`` loop guards on ``isfile``, so an absent
    pointer is an UNMASKED pointer for every namespace already running, and the crew data
    home is writable at OS level. An agent in such a namespace can simply CREATE the
    pointer, and the gateway ``execve``s into whatever it names at its next start.
    Publishing the document first makes the mask non-vacuous, so every namespace binds an
    empty file over the name from the outset.
    :data:`_LIVE_TARGET_PRECREATE_CONTENT` carries the absent-equivalence argument.

    Linux spawn path only, at the same site as the other materialisers: a Seatbelt deny is
    a path rule that already holds for a name which does not exist yet, so macOS needs
    nothing here. The LIVE data home only (``config_dir()``) — a stub under the deprecated
    spelling would be a file nothing reads — and an absent data home is left absent.

    **Fail-closed**, like the directory materialiser and for the same reason: launching
    with the pointer maskless is the exposure this exists to prevent. Never truncates and
    never removes — an existing regular file is left byte-for-byte alone, whether it holds
    a real pin or this stub. Returns the path if it published one.
    """
    try:
        root = str(config_dir())
    except Exception:  # pragma: no cover - defensive; a spawn must not fail on this
        logger.debug("could not resolve the crew data home for live-target masking")
        return None
    if not os.path.isdir(root):
        return None
    target = os.path.join(root, _LIVE_TARGET_LEAF)
    # ANY symlink refuses, dangling or resolving, and in ONE sentence. A resolving link
    # is the attack entry rather than just a dangling one: a mount follows its target, so
    # the mask would bind over the referent while the lexical name stayed an
    # agent-replaceable link in a writable directory. Refused before the isfile check,
    # exactly as the directory materialiser refuses before its isdir check.
    #
    # This refuses a SUPERSET of what the two generic helpers
    # (``_refuse_if_dangling_symlink`` then ``_refuse_if_symlink_leaf``) refused between
    # them, so nothing is admitted that they rejected. The pointer gets its own for two
    # reasons: those helpers say "the masked DIRECTORY <path> is a SYMLINK", which names
    # the wrong kind of thing for a JSON document an operator is about to go look at; and
    # the sentence is shared with ``live_target_pointer_unfitness`` so doctor's
    # pre-spawn warning and this refusal cannot come to describe one file two ways.
    _refuse_if_live_target_symlink(target)
    if os.path.exists(target):
        _refuse_unless_sole_regular_link(target)
        _note_established(established, target)
        return None
    # The temp is staged in a MASKED directory, never beside the target: the data-home
    # root is visible in every sandbox, so a temp there is a name a concurrent namespace
    # can ``link(2)`` — and a bind mask covers a path, not the inode behind it. The
    # staging directory is precreated (``_CREW_PRECREATE_HIDDEN_DIR_LEAVES``); it is a
    # direct child of the data home, so its own chain has no agent-writable component.
    staging = os.path.join(root, _LIVE_TARGET_STAGING_LEAF)
    try:
        os.makedirs(staging, mode=0o700, exist_ok=True)
    except OSError as exc:
        raise SandboxCeilingUnsealable(
            f"cannot create {staging} to stage the live-target pointer's mask target: {exc}"
        ) from exc
    if not stat.S_ISDIR(os.lstat(staging).st_mode):
        raise SandboxCeilingUnsealable(
            f"{staging} is not a directory; refusing to stage the live-target pointer's "
            "mask target through it"
        )
    if _publish_empty_ceiling(target, staging, content=_LIVE_TARGET_PRECREATE_CONTENT):
        # ``os.link`` publishes by adding a second name to the temp's inode, and the
        # temp is unlinked right after — so a link count above one here means someone
        # else linked the inode in the window, and a mask over THIS name would not
        # cover THEIR path to the bytes the gateway executes.
        _refuse_unless_sole_regular_link(target)
        _note_established(established, target)
        return target
    # A lost publish race is benign only if the winner cleared the same bar. Publishing is
    # ``os.link``, which fails EEXIST rather than clobbering, so the ordinary loser finds a
    # regular, singly-linked file here; anything else means the name is not maskable.
    try:
        _refuse_unless_sole_regular_link(target)
        # Validated on the winner's own terms, so established -- see the ceiling loop's
        # lost-publish arm for why a loser still counts as seen.
        _note_established(established, target)
        return None
    except FileNotFoundError:
        pass
    raise SandboxCeilingUnsealable(
        f"cannot give the live-target pointer's mask a mount target at {target}. "
        "Launching anyway would leave the pointer maskless in every agent namespace, "
        "where writing it selects the code the gateway starts next."
    )


def _refuse_if_live_target_symlink(target: str) -> None:
    """Refuse the spawn when the live-target pointer's path is a symlink of any kind.

    One check for both link shapes, because both fail the same way: a mask binds over the
    path a link RESOLVES to, so the link's own name stays a writable entry in the data
    home and a sandboxed process can replace it with a pin of its own. A dangling link is
    the same hole with the referent missing.

    Deliberately NOT the shared ``_refuse_if_symlink_leaf``: its sentence names "the
    masked directory", and this target is a JSON document. It also has to be the sentence
    :func:`live_target_pointer_unfitness` reports, so the pre-spawn warning and the
    refusal stay one string.

    Refused rather than removed: ``lstat`` then ``unlink`` is not atomic, so removing it
    here would race whoever put it there.
    """
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SandboxCeilingUnsealable(
            f"cannot stat the live-target pointer {safe_terminal_line(target)} to check "
            f"for a symlink: {safe_terminal_line(str(exc))}"
        ) from exc
    if not stat.S_ISLNK(info.st_mode):
        return
    points_at = "(unreadable)"
    with contextlib.suppress(OSError):
        points_at = os.readlink(target)
    raise SandboxCeilingUnsealable(_live_target_symlink_detail(target, points_at))


class LiveTargetUnfitness(NamedTuple):
    """Why the live-target pointer cannot be masked, and what fixes it.

    ``path`` is the pointer and ``detail`` is the SAME sentence
    :class:`SandboxCeilingUnsealable` carries for that shape -- see
    :func:`live_target_pointer_unfitness` for why the two must be one string.

    Deliberately no shape TAG beside them: every reader wants the sentence rather than a
    branch, and a constant a surface only MIGHT want is a guess about a caller that does
    not exist. The shapes are still told apart without matching prose, by comparing
    ``detail`` against the formatter that builds it, which is what the tests do. A surface
    that needs to branch can add the tag then, against a real requirement.
    """

    path: str
    detail: str


# Every interpolated value in the three sentences below goes through
# ``safe_terminal_line`` because BOTH readers print the sentence to a terminal verbatim:
# doctor through ``_print_wrapped`` and ``kirocrew chat`` through ``❌ {exc}``. One of
# those values is chosen by whoever planted the unfit pointer -- a SYMLINK's target comes
# back from ``os.readlink`` as arbitrary bytes -- so it can carry OSC/CSI sequences that
# retitle the window, write the clipboard, or redraw the lines around the diagnosis. The
# adversary is the sandboxed agent this pointer-masking exists to contain, and an operator
# running ``kirocrew doctor`` after "agents stopped starting" is the expected trigger, so
# the bytes are defused where the sentence is BUILT rather than at each surface that
# prints it -- a surface added later cannot forget.
#
# ``safe_terminal_line`` and not ``repr``: these sentences carry a path and a ``find``
# invocation the operator is meant to COPY, and ``repr`` would quote the whole value and
# escape its separators -- the same "two unusable fragments" outcome ``keep_tokens_whole``
# exists to prevent. Stripping control sequences leaves an ordinary path byte-identical.
def _live_target_irregular_detail(target: str) -> str:
    """The refusal sentence for a non-regular file at the pointer's path."""
    return (
        f"cannot mask {safe_terminal_line(target)}: a non-regular file (a link, FIFO, "
        "socket, or device node) is sitting at the live-target pointer's path. The "
        "launcher's isdir/isfile loops classify neither, so its mask would be silently "
        "skipped for every sandbox. Remove or replace it with a regular file."
    )


def _live_target_multilink_detail(target: str, links: int) -> str:
    """The refusal sentence for a pointer reachable under more than one name.

    Names the ``find`` invocation rather than only the condition: a hard link is left by
    ordinary operation (``cp -al``, rsnapshot, a dotfile manager), so the operator who
    meets this has no reason to know which OTHER path shares the inode, and without the
    command the remedy "remove the extra link" names no file to remove.
    """
    # ``shlex.quote`` per path, rather than one pair of quotes around the whole command: a
    # data home holding a space makes `find /opt/my data -samefile ...` a two-directory
    # search that answers a different question WITHOUT erroring, so the remedy has to
    # survive being pasted and not merely read correctly. Quoting applies to the DISPLAYED
    # text, so for the pathological case of a path holding a control byte the command is
    # illustrative rather than runnable; a terminal that cannot be driven matters more.
    return (
        f"cannot mask {safe_terminal_line(target)}: the live-target pointer has {links} "
        "hard links, so a mask over this name would leave another path to the same "
        "bytes unmasked. List the names under the data home with the command "
        f"find {shlex.quote(safe_terminal_line(os.path.dirname(target)))} -samefile "
        f"{shlex.quote(safe_terminal_line(target))} "
        "-- that searches the data home only, and the tools that leave a link here "
        "(snapshot and backup runs, a dotfile manager) usually keep theirs somewhere "
        "else, so if it reports just the pointer, run it again from the mount point "
        "holding it with -xdev added: a hard link cannot cross a filesystem, but it can "
        "sit anywhere on this one. Then remove the extra link(s) and restart."
    )


def _live_target_symlink_detail(target: str, points_at: str) -> str:
    """The refusal sentence for a symlink squatting the pointer's path.

    Wording of its own rather than the shared directory-leaf refusal's: that helper is
    reached for real directories too and says "the masked directory", which for
    ``live_target.json`` names the wrong kind of thing to an operator reading it.

    ``points_at`` is the one value here an adversary picks outright -- see the note above
    this group for why it is defused when the sentence is built.
    """
    return (
        f"cannot mask {safe_terminal_line(target)}: the live-target pointer is a "
        f"SYMLINK -> {safe_terminal_line(points_at)}. "
        "A mask binds over the link's target, not the name, so the name stays "
        "replaceable in a writable directory and a sandboxed process could point it "
        "at a checkout it controls. Replace it with a regular file, and restart."
    )


def _refuse_unless_sole_regular_link(target: str) -> None:
    """Raise unless *target* is a regular file with exactly one hard link.

    Two refusals, one reason each. A non-regular file (FIFO, socket, device): the
    launcher's ``isdir``/``isfile`` loops classify neither, so its mask would be skipped
    for every sandbox. A link count above one: a bind mask covers the NAME, so a second
    name on the same inode is an unmasked path to the bytes the gateway ``execve``s from
    — whether planted by a same-uid process in a namespace that could see a staging
    temp, or left by an operator's ``ln``. ``FileNotFoundError`` propagates so a
    publish-race caller can tell "gone" from "unfit".

    The sentences come from the module-level formatters so ``kirocrew doctor`` can report
    the same condition, in the same words, BEFORE a spawn refuses on it.
    """
    st = os.lstat(target)
    if not stat.S_ISREG(st.st_mode):
        raise SandboxCeilingUnsealable(_live_target_irregular_detail(target))
    if st.st_nlink != 1:
        raise SandboxCeilingUnsealable(_live_target_multilink_detail(target, st.st_nlink))


def live_target_pointer_unfitness() -> LiveTargetUnfitness | None:
    """Classify the LIVE live-target pointer the way a spawn would, WITHOUT spawning.

    ``None`` means nothing to report: a healthy pointer, an absent one (the materialiser
    publishes a stub for it), or no data home yet. Anything else is a shape that makes
    :func:`_materialize_live_target_mask_target` refuse, so it refuses EVERY Linux agent
    spawn on the host until an operator fixes it.

    It exists because the refusal is the operator's only notice today, and it arrives too
    late and in the wrong place: a hard link on a config file is ordinary operation for a
    snapshot tool (``cp -al``, rsnapshot) and for a dotfile manager, so the condition
    appears without anybody doing anything wrong, and the first symptom is that agents
    stop starting. ``kirocrew doctor`` is where an operator looks for that, and this is
    the read that lets it answer.

    Shares the refusal's own sentences rather than paraphrasing them, so the pre-spawn
    warning and the post-refusal error cannot drift into describing the same file two
    different ways — and so a reworded remedy reaches both surfaces at once.

    Read-only and total: it never creates, moves or removes anything, and a data home it
    cannot resolve or stat is reported as nothing rather than as a fault, because doctor
    must not turn its own probe failure into a verdict about the host.

    The POINTER itself is the exception: if it exists but cannot be stat'd the ``OSError``
    propagates rather than reading as fit, because ``None`` here means "nothing to
    report" and a pointer whose shape is unknown may still refuse every spawn. Doctor
    renders that as "could not check". Absent is the one genuinely fit failure to stat.
    """
    try:
        root = str(config_dir())
    except Exception:  # pragma: no cover - defensive; doctor must survive a bad home
        logger.debug("could not resolve the crew data home for live-target fitness")
        return None
    if not os.path.isdir(root):
        return None
    target = os.path.join(root, _LIVE_TARGET_LEAF)
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        # Absent is FIT: the materialiser publishes the absent-equivalent stub, which
        # is the whole reason that function exists.
        return None
    except OSError:
        # NOT ``return None``: None is this function's word for FIT, and an unreadable
        # pointer is not fit -- it is unknown. Swallowing it would make doctor print
        # nothing at all for a pointer it cannot classify, which reads as "checked,
        # healthy" while every Linux spawn may still refuse on it. Doctor's own caller
        # turns the raise into "could not check (...)", which is the honest answer and the
        # one its docstring promises. Only the ABSENT case above is genuinely fit.
        logger.debug("could not stat the live-target pointer %s", target, exc_info=True)
        raise
    if stat.S_ISLNK(st.st_mode):
        points_at = "(unreadable)"
        with contextlib.suppress(OSError):
            points_at = os.readlink(target)
        return LiveTargetUnfitness(
            path=target,
            detail=_live_target_symlink_detail(target, points_at),
        )
    if not stat.S_ISREG(st.st_mode):
        return LiveTargetUnfitness(
            path=target,
            detail=_live_target_irregular_detail(target),
        )
    if st.st_nlink != 1:
        return LiveTargetUnfitness(
            path=target,
            detail=_live_target_multilink_detail(target, st.st_nlink),
        )
    return None


def _md_notebook_degraded_mask_dirs() -> list[str]:
    """The md-notebook state directories to mask WHOLESALE because the app is degraded.

    Normally only the three secret leaves are masked, and that is deliberate: the same
    directory holds the vault clone data, which agents are meant to read. Masking it
    wholesale on a healthy host would hide the user's notes from every agent — the app's
    whole purpose.

    When a component of the chain is a planted link the calculus inverts. The Notes
    backend does not start while this list is non-empty (``apps/backend.py``), so nothing
    writes into the namespace-only stand-in this mask leaves, where a note would be lost on
    restart; hiding the directory costs nothing that still works. What it buys is the one exposure skipping leaves behind: a legacy staging orphan
    holding real PAT bytes that :func:`_sweep_one_md_notebook_state_dir` cannot delete
    through a link, and that the three leaf masks do not cover because its name is neither
    ``pat`` nor a state file. A directory mask covers every name inside it, orphans
    included.

    Masking is fail-safe in the direction that matters: the launcher binds an empty
    directory over the resolved path INSIDE the namespace only, so a link pointing
    somewhere unexpected costs that sandbox visibility of a path it should not have been
    reading, never anything on the host.
    """
    dirs: list[str] = []
    roots: list[str] = []
    try:
        roots.append(str(config_dir()))
    except Exception:  # pragma: no cover - defensive; a spawn must not fail on this
        logger.debug("could not resolve the crew data home for the md-notebook mask")
    try:
        home = Path.home()
        roots.extend(str(home / Path(prefix)) for prefix in _CREW_HOME_PREFIXES)
    except Exception:  # pragma: no cover - defensive
        logger.debug("could not resolve $HOME for the md-notebook mask")
    for root in dict.fromkeys(roots):
        state_dir = os.path.join(root, *_MD_NOTEBOOK_STATE_COMPONENTS)
        # Ask the predicate about a LEAF, exactly as the carve-out filter does, not about
        # the directory: it judges a path's PARENT chain, so passing the directory would
        # miss a link at the directory itself — the very case the sweep's final-component
        # refusal leaves unswept. Sharing the input shape keeps "withheld" and "masked"
        # literally the same decision rather than two that merely agree today.
        probe = os.path.join(state_dir, _MD_NOTEBOOK_STATE_FILES[0])
        if carveout_chain_has_planted_link(probe):
            dirs.append(state_dir)
    return dirs


def _sweep_legacy_md_notebook_temps() -> list[str]:
    """Remove pre-upgrade staging temps left BESIDE md-notebook's state files.

    Before the staging directory existed, all three state writers staged a sibling of
    their target: ``vaults.json.<hex>.tmp`` and ``settings.json.<hex>.tmp`` from
    ``git_ops.staged_temp_name``, and ``tmp<random>.tmp`` from ``atomic_write``'s
    ``mkstemp(dir=path.parent, suffix=".tmp")``. A SIGKILL in that window left a file
    holding the real PAT bytes at a name NO mask covers — not the three leaves, and not
    the staging directory. Materialising forward does not help: the exposure is an
    artefact already on disk, so on an upgraded host the exact bytes this carve-out
    exists to fence would stay readable by a same-uid agent forever.

    Runs on EVERY sandbox launch path, Linux namespace and macOS Seatbelt alike, and is
    deliberately NOT part of :func:`_materialize_md_notebook_mask_targets`. Materialising
    is Linux-only for a good reason — a Seatbelt deny is a path rule that holds for a name
    that does not exist yet — but that reasoning does not transfer to an orphan that DOES
    exist at a name no rule names: the Seatbelt profile denies the leaves and the staging
    directory, never an arbitrary ``*.tmp`` sibling, so skipping macOS would leave the
    token readable there forever. For the same asymmetry it sweeps EVERY crew-home
    spelling, live and legacy, while materialising touches only the live one.

    Every ``*.tmp`` DIRECT child of the state directory is such an orphan by
    construction: the state writers now stage inside the top-level staging directory, and
    note temps live beside their note inside ``vaults/<id>/``. Directories, links, and
    special files are left alone — only regular files are removed, judged by ``lstat`` so
    a link is never followed.

    **Fail-closed like the materialiser.** If an orphan cannot be removed the spawn is
    refused, naming the path: launching would hand the agent the PAT this whole mechanism
    is built to hide, and the operator can delete the file. Returns the paths it removed.
    """
    removed: list[str] = []
    # Every crew-home spelling, not just the live one. The mask covers BOTH prefixes
    # (see ``_crew_home_entries``), so a pre-upgrade orphan under an un-migrated or
    # rolled-back ``~/.kirocrew`` is exposed exactly as one under the current home. This
    # is the opposite requirement from MATERIALISING, which is live-home-only because a
    # stub in a home nothing reads would be a file nobody opens: a token already written
    # to the legacy home stays readable no matter which home is live now. De-duplicated so
    # a relocated ``KIROCREW_HOME`` coinciding with a prefix is swept once.
    roots: list[str] = []
    try:
        roots.append(str(config_dir()))
    except Exception:  # pragma: no cover - defensive; a spawn must not fail on this
        logger.debug(
            "could not resolve the crew data home for the md-notebook sweep", exc_info=True
        )
    try:
        home = Path.home()
        roots.extend(str(home / Path(prefix)) for prefix in _CREW_HOME_PREFIXES)
    except Exception:  # pragma: no cover - defensive
        logger.debug("could not resolve $HOME for the md-notebook sweep", exc_info=True)
    for root in dict.fromkeys(roots):
        removed.extend(_sweep_one_md_notebook_state_dir(root))
    return removed


#: The components between a crew data home and the md-notebook state directory. The
#: sweep descends them ONE AT A TIME from the home, so each name is resolved by the
#: kernel inside a directory this process already holds open.
_MD_NOTEBOOK_STATE_COMPONENTS: tuple[str, ...] = ("workspace", MD_NOTEBOOK_APP_NAME)


def _open_dir_anchored(anchor: str, components: tuple[str, ...]) -> int | None:
    """Open ``anchor/*components`` by descending one component at a time, or return None.

    Every step uses ``O_NOFOLLOW | O_DIRECTORY`` relative to the descriptor of the step
    above it, so the kernel refuses a link AT EACH component and resolves each name inside
    a directory this process is already holding. Opening the joined PATH instead would be
    unsound however carefully the chain was pre-checked: ``O_NOFOLLOW`` constrains only the
    FINAL component, so an intermediate directory swapped between the check and the open
    would silently redirect the whole descent, and the caller would then ``unlink`` inside
    a tree the agent chose. The descriptor chain closes that window structurally rather
    than narrowing it.

    ``anchor`` must be a trusted path (a crew data home), and the caller owns the returned
    descriptor. None means some component is absent, is not a directory, or is a link —
    all of which are "nothing safe to do here", never an error to escalate.
    """
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(anchor, flags)
    except OSError:
        return None
    for name in components:
        try:
            nxt = os.open(name, flags | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
        except OSError:
            os.close(fd)
            return None
        os.close(fd)
        fd = nxt
    return fd


#: The published signing key's leaf name under a crew data home. Named once because three
#: things key off it: the mask list, the legacy temp prefix below, and the staging
#: reconciliation that compares a staged file's inode against this file's.
_AUTH_STORE_PUBLISHED_KEY_LEAF: str = "token_signing.key"

#: The name shape a pre-upgrade signing-key publish left in the crew data-home root:
#: ``token_secret`` staged ``.<leaf>.<pid>.<hex>.tmp`` beside the key and published with
#: ``os.link``, so a SIGKILL between that link and the cleanup unlink leaves a file holding
#: the FULL signing key at a name no mask covers.
#:
#: Bounded to THIS prefix on purpose, and that bound is the load-bearing part. The
#: md-notebook sweep can take every ``*.tmp`` in its directory because that directory holds
#: nothing else; the data home root is shared, and ``atomic_write`` stages
#: ``tmp<random>.tmp`` there for many unrelated stores. A blanket sweep would unlink another
#: component's in-flight temp between its ``mkstemp`` and its rename and fail that write for
#: no reason -- the same hazard ``_CEILING_TEMP_PREFIX`` is skipped for one directory down.
#: A name carrying the leaf can only have come from this one publisher.
_AUTH_STORE_LEGACY_TEMP_PREFIX: str = f".{_AUTH_STORE_PUBLISHED_KEY_LEAF}."


def _reconcile_auth_store_staging_links() -> list[str]:
    """Drop a staging name that is a SECOND HARD LINK to the published signing key.

    Without this the hard-link refusal is a one-way door on a state the gateway's own
    publisher creates on purpose. ``token_secret`` publishes the key with ``os.link`` from
    a staged file and then unlinks the staged name; when the directory ``fsync`` after the
    link fails it deliberately KEEPS that name, as a recoverable second name to an inode
    whose only other name might not be durable yet, and a kill between the link and the
    unlink leaves the same shape. Either way ``token_signing.key`` has two names, the leaf
    is in :data:`_CREW_HARDLINK_REFUSED_LEAVES`, and every confined spawn then refuses --
    with no automatic way out, because the operator's only exit is to find and remove the
    name by hand.

    Reconciling is sound precisely BECAUSE the two names share an inode: the destination
    already resolves to the fully-written key, so the staged name carries no byte the
    destination does not. That identity is also the bound. A staged file from a publish
    still IN FLIGHT points at a different inode -- the key either does not exist yet or
    still names the previous one -- so a concurrent writer's temp is never touched.
    Anything that is not a regular file, and anything whose inode differs, is left alone.

    **The key's own directory entry is synced BEFORE the second name is dropped, and a
    sync the device refuses REFUSES the spawn.** Sharing an inode makes the second name
    redundant for reading, not for durability: the publisher keeps it exactly when
    ``fsync_dir`` on the key's parent failed, so at that moment the destination entry may
    not have reached the device and the staged name is the inode's one other reference. A
    crash after this function unlinked it, and before that entry commits, would leave the
    inode with no name at all -- the signing key gone and every dashboard session invalid.
    Syncing first is what makes dropping it lossless rather than probabilistic. Where a
    directory sync cannot be EXPRESSED (Windows has no directory descriptor, some network
    mounts reject it) ``fsync_dir`` returns quietly and the unlink proceeds, so the refusal
    is reserved for a device that actively refused the write -- a host whose storage is
    failing, where destroying the key's only durable name is the worse outcome.

    The sync runs only when a matching alias was actually found. A healthy home returns at
    the link-count test above, so this costs nothing on the ordinary spawn path.

    Runs BEFORE :func:`_refuse_aliased_masked_leaves`, which is the ordering this function
    exists to establish: cleanup that a refusal gates on must not sit behind it.

    A root, staging directory, or key it cannot open or stat is SKIPPED rather than
    escalated. Refusing here would fail every spawn for a layout that is merely unusual,
    and the refusal downstream still judges whatever link count survives.
    """
    removed: list[str] = []
    try:
        live_home = str(config_dir())
    except Exception:  # pragma: no cover - defensive; a spawn must not fail on this
        logger.debug("could not resolve the crew data home for the reconciler", exc_info=True)
        live_home = ""
    for root in _masked_crew_home_roots():
        try:
            key_info = os.lstat(os.path.join(root, _AUTH_STORE_PUBLISHED_KEY_LEAF))
        except OSError:
            # No published key here, so no staged name can be a second link to one.
            continue
        if not stat.S_ISREG(key_info.st_mode) or key_info.st_nlink < 2:
            continue
        dir_fd = _open_dir_anchored(root, (_AUTH_STORE_STAGING_LEAF,))
        if dir_fd is None:
            continue
        try:
            aliases: list[str] = []
            for name in os.listdir(dir_fd):
                try:
                    info = os.lstat(name, dir_fd=dir_fd)
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                if (info.st_dev, info.st_ino) != (key_info.st_dev, key_info.st_ino):
                    # A different inode: an in-flight staging write, or an unrelated file.
                    continue
                aliases.append(name)
            if not aliases:
                continue
            try:
                fsync_dir(root)
            except OSError as exc:
                detail = (
                    f"cannot sync {safe_terminal_line(root)} to make the token-signing "
                    f"key's own directory entry durable: {safe_terminal_line(str(exc))}. "
                    "The publisher kept a staging link as the key inode's second name "
                    "because that same sync failed, so dropping it now could leave the key "
                    "with no name at all after a crash, and keeping it leaves the masked "
                    "leaf with a link count the spawn path refuses on. Fix the device or "
                    "filesystem holding the data home, then restart."
                )
                if root == live_home:
                    raise SandboxCeilingUnsealable(detail) from exc
                logger.warning("SECURITY: %s", detail)
                continue
            for name in aliases:
                candidate = os.path.join(root, _AUTH_STORE_STAGING_LEAF, name)
                try:
                    os.unlink(name, dir_fd=dir_fd)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    detail = (
                        f"cannot remove the stale auth-store staging link "
                        f"{safe_terminal_line(candidate)}: {safe_terminal_line(str(exc))}. It "
                        "is a second name for the token-signing key, so the masked leaf has "
                        "a link count the spawn path refuses on, and leaving it would make "
                        "that refusal permanent. Remove it and restart."
                    )
                    if root == live_home:
                        raise SandboxCeilingUnsealable(detail) from exc
                    logger.warning("SECURITY: %s", detail)
                    continue
                logger.info(
                    "sandbox: removed a stale auth-store staging link at %s. The publish it "
                    "belonged to had already linked the key into place and that name is now "
                    "synced, so this name was a redundant second name for the same inode "
                    "and the masked leaf read as aliased.",
                    safe_terminal_line(candidate),
                )
                removed.append(candidate)
        finally:
            os.close(dir_fd)
    return removed


def _sweep_legacy_auth_store_temps() -> list[str]:
    """Remove pre-upgrade signing-key staging temps left in the crew data-home root.

    The publisher stages inside :data:`_AUTH_STORE_STAGING_LEAF`, which is masked as a
    whole directory. A temp written before that directory existed sits in the data-home
    root instead, at a name no mask covers, holding the bytes that sign every dashboard
    token -- so on an upgraded host it stays readable by a same-uid agent indefinitely.
    Masking forward cannot reach it: the exposure is an artefact already on disk.

    Swept on every launch path, Linux namespace and macOS Seatbelt alike, and across every
    crew-home spelling rather than only the live one, for the reason
    :func:`_sweep_legacy_md_notebook_temps` gives: a Seatbelt profile denies named paths,
    never an arbitrary temp name, and a key already written under a rolled-back home stays
    readable whichever home is live now.

    Only regular files are removed, judged by ``lstat`` through a pinned descriptor so a
    link is never followed. A root that cannot be opened is skipped rather than escalated:
    skipping removes the deletion hazard entirely, while refusing would fail every spawn on
    a host whose layout is merely unusual. A file that matches and cannot be removed DOES
    refuse the spawn, naming the path -- launching would hand the agent the signing key.

    A match is only removed once it is known not to be the key inode's last name, because
    the pre-upgrade publisher kept exactly this name when its own directory sync failed:

    * the published key is ABSENT -- nothing is removed, and the condition is REPORTED.
      Whether this name is the inode's only one cannot be told apart from a staged write
      that never published, or from a file created inside a sandbox: a home the install does
      not use is masked by nothing while it is absent, so anything in it may be the governed
      process's own work. Removing risks destroying a key an operator can still recover by
      renaming it, and refusing would let one ``touch`` stop every launch on the host.
    * the published key is present and shares this inode -- ``fsync_dir`` on the root first,
      then remove. Sharing the inode means the key's own entry may not have reached the
      device yet, so this is the protocol
      :func:`_reconcile_auth_store_staging_links` uses for the same reason.
    * the published key is present with a different inode -- removed. It is an older or
      never-published staged copy, and the live key is reachable by its own name.

    A sync or an unlink this pass cannot perform REFUSES the spawn in the live data home and
    is reported elsewhere, for the reason
    :func:`_refuse_multilinked_credential_leaves` gives at length: the live home exists and
    is therefore masked, so nothing inside it was planted from a sandbox, while an unused
    home is neither.
    """
    removed: list[str] = []
    try:
        live_home = str(config_dir())
    except Exception:  # pragma: no cover - defensive; a spawn must not fail on this
        logger.debug("could not resolve the crew data home for the legacy sweep", exc_info=True)
        live_home = ""
    for root in _masked_crew_home_roots():
        dir_fd = _open_dir_anchored(root, ())
        if dir_fd is None:
            continue
        try:
            for name in os.listdir(dir_fd):
                if not name.startswith(_AUTH_STORE_LEGACY_TEMP_PREFIX) or not name.endswith(".tmp"):
                    continue
                candidate = os.path.join(root, name)
                try:
                    info = os.lstat(name, dir_fd=dir_fd)
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                try:
                    key_info = os.lstat(_AUTH_STORE_PUBLISHED_KEY_LEAF, dir_fd=dir_fd)
                except FileNotFoundError:
                    # No key at its own name, so this name may be the inode's only one --
                    # or it may be a file created from inside a sandbox, because a home the
                    # install does not use is masked by nothing while it is absent. Those
                    # two are indistinguishable here, and refusing would let the governed
                    # process stop every launch on the host with one touch(1). So report and
                    # leave it: an operator can rename it onto the signing-key name to keep
                    # every current session, or delete it to mint a fresh key.
                    logger.warning(
                        "SECURITY: leaving the legacy auth-store staging temp %s in place. "
                        "%s is absent, so this name may be the only one left for the inode "
                        "holding the token-signing key, and removing it could destroy a key "
                        "that is still recoverable. It is not masked, so treat its bytes as "
                        "exposed to anything running as this user: rename it onto the "
                        "signing-key name to keep every current dashboard session, or delete "
                        "it to mint a fresh key on the next start.",
                        safe_terminal_line(candidate),
                        safe_terminal_line(os.path.join(root, _AUTH_STORE_PUBLISHED_KEY_LEAF)),
                    )
                    continue
                except OSError:
                    # The key's own entry cannot be classified, so whether this name is its
                    # last one is unknown. Skip rather than unlink: the same fail-closed
                    # choice the descent above makes, and the mask still covers the key.
                    continue
                if (info.st_dev, info.st_ino) == (key_info.st_dev, key_info.st_ino):
                    # This name is the published key's SECOND name, which the pre-upgrade
                    # publisher kept exactly when its own directory sync failed. So the
                    # key's own entry may not have reached the device, and this is the
                    # inode's one other reference: sync first, on the same protocol the
                    # staging reconciler uses.
                    try:
                        fsync_dir(root)
                    except OSError as exc:
                        detail = (
                            f"cannot sync {safe_terminal_line(root)} before removing the "
                            f"legacy auth-store staging temp {safe_terminal_line(candidate)}, "
                            f"which is a second name for the token-signing key's own inode: "
                            f"{safe_terminal_line(str(exc))}. Dropping it now could leave the "
                            "key with no name at all after a crash, and keeping it leaves a "
                            "cleartext key at a name no mask covers. Fix the device or "
                            "filesystem holding the data home, then restart."
                        )
                        if root == live_home:
                            raise SandboxCeilingUnsealable(detail) from exc
                        logger.warning("SECURITY: %s", detail)
                        continue
                try:
                    os.unlink(name, dir_fd=dir_fd)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    detail = (
                        "cannot remove the legacy auth-store staging temp "
                        f"{safe_terminal_line(candidate)}: {safe_terminal_line(str(exc))}. A "
                        "pre-upgrade publish staged it beside the signing key, so it can hold "
                        "the key that signs every dashboard token at a name no mask covers. "
                        "Launching anyway would leave it readable inside every agent "
                        "namespace -- delete it and retry."
                    )
                    if root == live_home:
                        raise SandboxCeilingUnsealable(detail) from exc
                    logger.warning("SECURITY: %s", detail)
                    continue
                logger.warning(
                    "SECURITY: removed a legacy auth-store staging temp at %s. A "
                    "pre-upgrade publish staged it beside the signing key, where no sandbox "
                    "mask covers it, so it may have held the token-signing key in "
                    "cleartext. Treat that key as exposed to anything that ran as this "
                    "user; restart the gateway to mint a fresh one if in doubt.",
                    safe_terminal_line(candidate),
                )
                removed.append(candidate)
        finally:
            os.close(dir_fd)
    return removed


def _sweep_one_md_notebook_state_dir(root: str) -> list[str]:
    """Remove the legacy staging orphans in ONE md-notebook state directory.

    The guards here are stricter than the sibling materialiser's for a concrete reason:
    that function only ever creates, while this one ``unlink``s, and an unlink through an
    attacker-chosen directory is irreversible.

    The intermediate components (``workspace/``, ``workspace/md-notebook``) are
    agent-writable, so a RESOLVING link planted at one of them would otherwise make this
    sweep list and delete ``*.tmp`` files in a tree the agent picked. The descent from
    *root* is therefore anchored and per-component (:func:`_open_dir_anchored`), and every
    ``lstat`` and ``unlink`` is issued against the pinned final descriptor.

    A root whose descent fails is SKIPPED, not escalated to a spawn refusal: skipping
    removes the deletion hazard completely, and refusing would fail every agent spawn on a
    host whose layout is merely unusual rather than hostile — one that symlinks the legacy
    ``~/.kirocrew`` at the new home, say. An orphan under such a root survives unswept,
    which is why the same predicate that degrades the app also masks the whole state
    directory (:func:`_md_notebook_degraded_mask_dirs`): the orphan is then hidden from the
    sandbox even though it could not be deleted.
    """
    removed: list[str] = []
    state_dir = os.path.join(root, *_MD_NOTEBOOK_STATE_COMPONENTS)
    dir_fd = _open_dir_anchored(root, _MD_NOTEBOOK_STATE_COMPONENTS)
    if dir_fd is None:
        # Absent, not a directory, or a link at some component. Nothing safe to do here.
        return removed
    try:
        names = os.listdir(dir_fd)
        protected = set(_MD_NOTEBOOK_STATE_FILES)
        for name in names:
            if not name.endswith(".tmp") or name in protected:
                continue
            # ``_publish_empty_ceiling`` stages ITS temp in the target's parent — this
            # very directory — under this prefix, and publishes with ``os.link``. Two
            # concurrent spawns would otherwise let one's sweep unlink the other's
            # in-flight temp between its ``mkstemp`` and its ``link``, failing that spawn
            # for no reason. These are gateway-owned and short-lived, never the legacy
            # orphans this sweep is for.
            if name.startswith(_CEILING_TEMP_PREFIX):
                continue
            candidate = os.path.join(state_dir, name)
            # Both syscalls go through the pinned descriptor, so the name is resolved
            # inside the directory this function verified — never through a component
            # something swapped after the check.
            try:
                if not stat.S_ISREG(os.lstat(name, dir_fd=dir_fd).st_mode):
                    continue
            except OSError:
                continue
            try:
                os.unlink(name, dir_fd=dir_fd)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise SandboxCeilingUnsealable(
                    f"cannot remove the legacy md-notebook staging temp {candidate}: "
                    f"{exc}. It was staged beside the state files by a pre-upgrade "
                    "writer, so it can hold the real PAT bytes at a name no mask covers. "
                    "Launching anyway would leave it readable inside every agent "
                    "namespace — delete it and retry."
                ) from exc
            logger.warning(
                "SECURITY: removed a legacy md-notebook staging temp at %s. A pre-upgrade "
                "writer staged it beside the state files, where no sandbox mask covers "
                "it, so it may have held the GitHub token in cleartext. Treat that token "
                "as exposed to anything that ran as this user and rotate it if in doubt.",
                candidate,
            )
            removed.append(candidate)
    finally:
        os.close(dir_fd)
    return removed


def _materialize_md_notebook_mask_targets(established: list[str] | None = None) -> list[str]:
    """Create md-notebook's absent state files and staging dir so their masks can mount.

    The NESTED counterpart to :func:`_materialize_maskable_dirs`, whose plain ``mkdir``
    is sound only for a DIRECT child of the data home. These leaves sit under
    ``workspace/md-notebook/``, and that restriction names exactly why the difference
    matters: the intermediate components are agent-writable, so a RESOLVING link planted
    at one of them would land the materialised files under an attacker-chosen tree while
    the launcher masks the lexical path. Every chain this function walks therefore goes
    through :func:`atomic_write.refuse_linked_parent` BEFORE any
    ``mkdir``, because ``mkdir`` itself follows a planted link.

    Closes the sibling gap named at :data:`_CREW_PRECREATE_READONLY_DIR_LEAVES`: the
    ``sensitive_files`` mask loop guards on ``isfile``, so an ABSENT leaf gets no mask at
    all. :data:`_MD_NOTEBOOK_PRECREATE_CONTENT` carries the per-leaf absent-equivalence
    argument that gap note requires.

    Runs on the Linux spawn path only, at the same site as
    :func:`_materialize_sealable_ceilings`; the macOS profile needs nothing here because
    Seatbelt denies are path rules that hold for names that do not exist yet.
    **Fail-closed** for the same reason the ceiling materialiser is: launching anyway
    would run the agent with a mask the launcher silently skipped. Creation happens only
    under the LIVE data home (``config_dir()``) — a stub in the deprecated spelling would
    be a file nothing reads — and an absent data home root is left absent, mirroring
    ``_sealable_absent_ceilings``.

    Never truncates and never removes: an existing regular file is left byte-for-byte
    alone, and ``EEXIST`` outcomes are the benign race.
    """
    created: list[str] = []
    try:
        root = str(config_dir())
    except Exception:  # pragma: no cover - defensive; a spawn must not fail on this
        logger.debug("could not resolve the crew data home for md-notebook masking", exc_info=True)
        return created
    if not os.path.isdir(root):
        return created

    # The staging DIRECTORY is not created here: it is a direct child of the data home, so
    # ``_materialize_maskable_dirs`` above already covers it under its own rule. The legacy
    # sweep is not here either — it must run on the macOS path too, so both launch sites
    # call it directly.
    for leaf, content in _MD_NOTEBOOK_PRECREATE_CONTENT.items():
        target = os.path.join(root, leaf)
        # A link planted at an intermediate component means this process cannot tell which
        # directory the write would reach, so the leaf is SKIPPED rather than materialised —
        # and the spawn proceeds. Refusing here would let one optional app's on-disk layout
        # take every sandboxed process on the host down with it: an operator who symlinks
        # ``workspace/`` to another disk would find no agent could start, over a Notes file
        # they may never have created.
        #
        # Skipping is safe only because it is keyed off the SAME predicate that withholds
        # the carve-out (:func:`carveout_chain_has_planted_link`, applied in
        # :func:`app_backend_visible_targets`). While the condition holds the backend cannot
        # write this state at all, so a leaf left unmaterialised — and therefore unmasked —
        # has nothing to expose. The two must never be decided separately: granting the
        # carve-out while skipping materialisation is exactly the hole this function exists
        # to close, which is why a test pins them together.
        if carveout_chain_has_planted_link(target):
            continue
        if os.path.exists(target):
            # Present is acceptable only as a REGULAR file, and ``lstat`` is what
            # decides — so this is also the LINK refusal. A mount RESOLVES its target,
            # so a resolving link here would mask the referent while the lexical name
            # stayed an agent-replaceable link in a writable parent: swap it after
            # launch and a later PAT write publishes to an unmasked name inside the
            # live namespace. (``atomic_write`` deliberately ALLOWS a leaf link,
            # because ``os.replace`` does not follow the final component; a mount
            # target has the opposite requirement.) A FIFO, socket, or device node is
            # refused for a different reason: the launcher's hiding loops classify with
            # ``isdir``/``isfile`` and a special file matches NEITHER, so its mask
            # would be silently skipped for the whole sandbox.
            if not stat.S_ISREG(os.lstat(target).st_mode):
                raise SandboxCeilingUnsealable(
                    f"the md-notebook state mask target {target} exists but is not a "
                    "regular file (a link, or a special file). The mask would bind "
                    "over a referent, or be skipped by the launcher's isdir/isfile "
                    "loops. Remove or replace it with a regular file."
                )
            _note_established(established, target)
            continue
        parent = os.path.dirname(target)
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            raise SandboxCeilingUnsealable(
                f"cannot create {parent} to give the md-notebook state mask a mount "
                f"target: {exc}. Launching anyway would leave {target} maskless in every "
                "agent namespace once the Notes backend creates it."
            ) from exc
        if _publish_empty_ceiling(target, parent, content=content):
            created.append(target)
            _note_established(established, target)
        else:
            # A lost publish race is benign ONLY when the winner clears the SAME bar an
            # existing target had to: a regular file. ``lstat`` judges it, so this also
            # catches a DANGLING leaf link — ``exists()`` is False for one, so it reaches
            # the publish, where ``os.link`` fails EEXIST on the link's own name. And it
            # catches an agent racing ``mkfifo``/``symlink`` between the validation and
            # the publish, which accepting bare existence here would have let through.
            try:
                winner = os.lstat(target)
            except OSError as exc:
                raise SandboxCeilingUnsealable(
                    f"cannot re-check the md-notebook state mask target {target} after a "
                    f"publish race: {exc}"
                ) from exc
            if not stat.S_ISREG(winner.st_mode):
                raise SandboxCeilingUnsealable(
                    f"the md-notebook state mask target {target} is not a regular file "
                    "after the publish (a link, or a special file that won the race), so "
                    "the mask would bind over a referent or be skipped by the launcher's "
                    "isdir/isfile loops. Remove or replace it with a regular file."
                )
            # Validated and accepted, so established on the same footing as one this
            # pass published itself.
            _note_established(established, target)
    return created


def _migrate_md_notebook_legacy_state() -> list[str]:
    """Run the Notes builtin's one-time legacy-state move before this sandbox launches.

    Spawn prep runs it, not only the Notes backend's own spawn, so the first pass and its
    marker that closes promotion happen as early as possible, before an agent of this build
    gets a shell on a fresh upgrade. After that it is one ``stat``.

    The move itself lives in :mod:`kiro_crew.md_notebook_state`, which imports no app code
    or aiohttp. It runs against this module's ``config_dir()``, the same home the
    materialiser fills.

    **A failed move does not stop the spawn.** It is one optional app's state, so a copy or
    read error, a full disk, an unwritable marker or an extra hard link on the PAT is
    logged and the pass stays unsealed for the next spawn to retry. No agent can plant a
    bare state name meanwhile: :func:`_materialize_md_notebook_mask_targets` runs right
    after this and stubs every bare name, so its mask always mounts, and the Notes backend
    does not start until the pass is sealed (``sealed_default_legacy_state`` in
    ``apps/backend.py``). An absent data home is left absent: there is nothing to move.
    """
    # Local, not top-level: every module-level name here is an owner name the
    # ``kiro_crew.sandbox`` facade inventory forwards, and a module alias is not one.
    from kiro_crew import md_notebook_state as mns

    try:
        root = str(config_dir())
    except Exception:  # pragma: no cover - defensive; a spawn must not fail on this
        logger.debug("could not resolve the crew data home for the Notes move", exc_info=True)
        return []
    if not os.path.isdir(root):
        return []
    state_dir = Path(root) / mns.STATE_DIR_LEAF
    if carveout_chain_has_planted_link(str(state_dir / mns.MIGRATION_MARKER)):
        # The same skip the materialiser takes for this chain: this process cannot tell
        # where a write would land, so it writes nothing. The Notes backend's own writer
        # refuses the same chain, so nothing on this host writes Notes state at all.
        return []
    try:
        return mns.migrate_legacy_state(state_dir, Path(root).joinpath(*mns.LEGACY_DIR_PARTS))
    except OSError as exc:
        # Log and continue: the stubs planted next keep every bare name masked, and the
        # Notes backend stays down until a later pass seals.
        logger.warning(
            "the Notes builtin's one-time move of its old state into %s did not finish: %s. "
            "Notes stays off until it does; the next spawn retries it. Fix the cause (disk "
            "space, permissions, or an extra hard link on the PAT).",
            state_dir,
            exc,
        )
        return []


def _member_records_move() -> bool:
    """Run the crewmate records' one-time move out of ``trust/`` before this sandbox launches.

    Spawn prep runs it so the move and its marker land before an agent of this build gets a
    shell on a fresh upgrade; after that it is one ``stat``. The move lives in
    :mod:`kiro_crew.member_records`, which every gateway-side reader also calls.

    **A move that did not finish refuses the spawn** (:class:`SandboxCeilingUnsealable`)
    whether or not the old layout holds a record now.
    ``trust/`` is writable inside every sandbox, so a process launched before the seal
    could write or replace a record there, and the next pass would promote it into the
    directory the gateway trusts. The refusal names the cause so the
    operator can fix it (disk space, permissions, a link at ``trust/<kind>``); the next
    spawn retries the move. Inside a sandbox it is a no-op: the gateway's own spawn already
    decided.
    """
    # Local, not top-level, for the reason the Notes move above gives.
    from kiro_crew import member_records

    if member_records._inside_sandbox() or member_records.ensure_migrated():
        return True
    # Unsealed refuses whatever the old layout holds now: a sandbox launched before the
    # seal could write a record there for the next pass to promote. A fresh data home is
    # not an exception that needs carving out, because the move seals one at once.
    pending = member_records.outstanding_kinds()
    what = f"crewmate {', '.join(pending)} records" if pending else "crewmate records"
    raise SandboxCeilingUnsealable(
        f"not launching a sandbox: the one-time move of {what} out of "
        f"{config_dir() / 'trust'} into {config_dir() / member_records.MEMBER_RECORDS_LEAF} "
        f"did not finish ({member_records.last_failure or 'unknown cause'}). Until it does, a "
        "sandboxed process could change records the gateway is about to trust. Fix the cause "
        "and start the session again; the move is retried first."
    )
