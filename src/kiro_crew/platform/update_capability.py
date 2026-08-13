"""The one derivation of install shape → update capability.

Whether an update *can* be applied by the running process is a property of how
this copy was installed. Whether it *should* be applied without asking is a
property of channel and policy. Conflating the two is what makes an update UI
render impossible actions: a shape question (``isDesktop``) gets asked to answer
a consent question.

Every caller — the dashboard check endpoint, the apply endpoint, the CLI update
command — reads its answer from :func:`derive_capability` instead of probing the
environment itself, so the derivation cannot drift between them.

``mode`` describes the designed consent posture for a shape, not the behavior of
the legacy ``auto_update`` config key, which still drives an unattended
boot-time apply on a git checkout and is governed elsewhere.

Two fields specified for this contract are deliberately absent: ``state`` and
``progress`` describe an apply/drain lifecycle that does not exist yet, and
serving them as constants would advertise transitions a consumer could poll for
forever.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, replace
from typing import Any

from kiro_crew._bootstrap import _source_checkout_root
from kiro_crew.beacon import distribution
from kiro_crew.platform_compat import trusted_system_bin

#: Who owns replacing this install's bytes.
MANAGED_BY_ELECTRON = "electron"
MANAGED_BY_KIROCREW = "kirocrew"
MANAGED_BY_GIT = "git"
MANAGED_BY_CONTAINER = "container"
MANAGED_BY_NONE = "none"

#: Consent posture. ``notify`` tells the user; it never applies on its own.
MODE_AUTO = "auto"
MODE_CONSENT = "consent"
MODE_NOTIFY = "notify"
MODE_NONE = "none"

#: Lifecycle of a *check*. ``succeeded`` is the only value that makes an
#: ``update_available`` verdict authoritative — see the honesty pair below.
CHECK_UNCHECKED = "unchecked"
CHECK_CHECKING = "checking"
CHECK_SUCCEEDED = "succeeded"
CHECK_FAILED = "failed"
CHECK_DEFERRED = "deferred"

#: Machine-readable failure classes for a check that could not reach a verdict.
#: A consumer that does not recognise a code must still render "the check
#: failed" rather than falling through to success.
ERR_FEED_UNREACHABLE = "feed_unreachable"
ERR_FEED_MALFORMED = "feed_malformed"
ERR_GIT_FETCH_FAILED = "git_fetch_failed"
ERR_GIT_READ_FAILED = "git_read_failed"
ERR_VERSION_UNPARSEABLE = "version_unparseable"
ERR_UNKNOWN = "unknown"

#: Why a shape has no verdict of its own. A deferral is not a failure: a desktop
#: bundle reporting "the app updates itself" has not malfunctioned, and
#: rendering it as an error is its own lie.
UNAVAILABLE_MANAGED_BY_APP = "managed_by_app"
UNAVAILABLE_MANAGED_BY_IMAGE = "managed_by_image"

#: Human copy for the shapes this gateway does not update, keyed by
#: ``unavailable_reason``. Kept here rather than beside each consumer so the two
#: surfaces that show it cannot drift apart.
EXTERNALLY_MANAGED_MESSAGES = {
    UNAVAILABLE_MANAGED_BY_APP: (
        "Update via the desktop app's built-in updater (About → Check for updates)."
    ),
    UNAVAILABLE_MANAGED_BY_IMAGE: "Update by pulling a newer image (docker pull).",
}

#: Distribution stamps whose code is replaced by something other than this
#: gateway. The desktop bundles EMBED this backend, so they execute this module
#: and must still defer: reading the CLI feed there compares against the wrong
#: release stream and then lights a badge pointing at a panel that says
#: "up to date".
#:
#: ``deb`` and ``rpm`` are the same desktop app packaged for Linux, so they
#: belong here rather than falling through to the wheel branch: their bytes are
#: replaced by the app's updater handing a package to dpkg or rpm, and a wheel
#: classification would offer this gateway's apply endpoint for a tree it cannot
#: touch.
_ELECTRON_DISTRIBUTIONS = frozenset({"dmg", "appimage", "deb", "rpm"})
_CONTAINER_DISTRIBUTIONS = frozenset({"docker"})

#: Every stamp whose updates are owned elsewhere. Public because the policy
#: floor has to answer the badge question before any check has produced a
#: capability, and the stamp is the one input available with no I/O.
EXTERNALLY_MANAGED_STAMPS = _ELECTRON_DISTRIBUTIONS | _CONTAINER_DISTRIBUTIONS

_GIT_TIMEOUT_SECS = 5

#: Environment variables that point git at a DIFFERENT repository than ``-C``
#: names. Left in place, ``GIT_DIR`` alone makes ``rev-parse --show-toplevel``
#: answer for an unrelated tree, which would classify a non-checkout as a
#: checkout and then let the apply path run ``git reset --hard`` against it.
_GIT_LOCATION_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
)


def _git_toplevel(root: str) -> str | None:
    """The working-tree root git reports for *root*, or ``None`` if it cannot say.

    ``None`` means INDETERMINATE, not "no": git is missing from PATH, the call
    timed out on a stale network mount, or the answer came back in a form this
    process cannot resolve to a real directory. Those cases must not be read as
    "this is not a checkout" — see :func:`is_git_worktree`.
    """
    env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_ENV}
    # PATH is NOT trusted to name the binary. A gateway's PATH legitimately leads
    # with agent-writable directories (a worktree venv's `bin`, `~/.local/bin`), and
    # this runs unattended on the boot check — a bare "git" would let a planted shim
    # execute with the gateway's environment. `None` (git is not in a fixed system
    # directory) is INDETERMINATE, the same as any other answer this probe cannot
    # obtain: `is_git_worktree` then falls back to the on-disk repository markers, so
    # a host that keeps git outside those directories loses the authoritative answer
    # but never loses the update path.
    git_bin = trusted_system_bin("git")
    if git_bin is None:
        return None
    try:
        proc = subprocess.run(
            # quotePath=false keeps a non-ASCII path from coming back as octal
            # escapes, which no path comparison could then resolve.
            [git_bin, "-c", "core.quotePath=false", "-C", root, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            # A path on Linux is BYTES, so a checkout can live somewhere that is not
            # valid UTF-8. Strict decoding raises there — caught below as a
            # ValueError, so it never escapes, but the answer would be thrown away
            # and detection would fall back to the on-disk marker heuristic.
            # surrogateescape round-trips those bytes instead, so git's
            # authoritative answer survives and `os.path.samefile` can still anchor
            # it to the requested root.
            errors="surrogateescape",
            timeout=_GIT_TIMEOUT_SECS,
            env=env,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        # ValueError belongs here: a NUL byte in the path raises it rather than
        # OSError, and an uncaught one would propagate out of the derivation.
        return None
    if proc.returncode != 0:
        return None
    top = proc.stdout.strip()
    if not top or any(ch in top for ch in "\n\r\x00"):
        return None
    if not os.path.isdir(top):
        # git answered in a form this process cannot resolve — an MSYS-style
        # ``/c/Users/...`` under a Win32 interpreter, or a mangled encoding.
        return None
    return top


def _looks_like_a_repository(root: str) -> bool:
    """Does *root* carry the on-disk markers of a git working tree's own root?

    The fallback for when git cannot answer. Stricter than the presence of a
    ``.git`` entry, which is all the check this replaced ever asked: a real
    checkout has either a ``.git`` DIRECTORY containing ``HEAD``, or — for a
    linked worktree or a submodule — a ``.git`` FILE holding a ``gitdir:``
    pointer. A leftover or fabricated ``.git`` satisfies neither, so it is refused
    here as well as on the primary path.
    """
    entry = os.path.join(root, ".git")
    if os.path.isdir(entry):
        return os.path.exists(os.path.join(entry, "HEAD"))
    if os.path.isfile(entry):
        try:
            with open(entry, encoding="utf-8", errors="replace") as handle:
                head = handle.read(4096)
        except OSError:
            return False
        # git's own rule: the file STARTS with the marker. A leading newline is not
        # whitespace to be tolerated here — git rejects it, so a file that merely
        # contains the marker further in must not read as a checkout. The BOM is
        # stripped because some Windows git builds write one and str.strip() would
        # not remove it, which would refuse a legitimate worktree.
        head = head.lstrip("\ufeff")
        if not head.startswith("gitdir:"):
            return False
        target = head[len("gitdir:") :].strip()
        if not target:
            return False
        # The pointer has to point somewhere: a file that merely begins with the
        # marker is as stray as one that does not, and reading it as a checkout
        # would hand the apply path a tree git cannot operate on.
        pointed = target.splitlines()[0].strip()
        if not os.path.isabs(pointed):
            pointed = os.path.join(root, pointed)
        return os.path.isdir(pointed)
    return False


def is_git_worktree(root: str) -> bool:
    """Is *root* itself the top level of a git working tree?

    Asks git and anchors the answer to *root*, so a linked worktree and a
    submodule each answer for themselves while a directory whose ANCESTOR is a
    repository is rejected — a venv nested in a project tree, or a home
    directory that is itself a dotfiles repo, is not this install's checkout, and
    the boot-time git apply would otherwise reset a tree unrelated to it.

    **When git cannot answer, this degrades to the on-disk markers of a
    repository rather than to "not a checkout".** The distinction is
    load-bearing: misreading a real checkout as a wheel install takes away its
    update path entirely, and a broken updater cannot ship its own fix. Missing
    git binary, a hang on a stale mount, an unreadable answer, and a refusal such
    as dubious ownership all land here, and all of them updated fine on the
    weaker check this replaced. Stderr is deliberately NOT pattern-matched to
    separate a refusal from an absence: git's messages are localized, so the
    match would silently stop working under a non-English locale.

    Both paths stay ANCHORED at *root*, so ancestor capture is rejected either
    way. What neither path can do is tell this install's own checkout from an
    unrelated repository that ``KIROCREW_PROJECT_DIR`` happens to name — that
    needs a signal other than the path, which is what
    :func:`running_from_checkout` supplies on top of this probe.
    """
    if not root or "\x00" in root:
        return False
    # A relative path resolves against this process's working directory. That is
    # inherited behavior, not a choice — the check this replaced joined `.git`
    # onto the same relative value — so it is made explicit here rather than
    # rejected, which would take the update path away from an install configured
    # that way. A leading dash would be read by git as an option.
    root = os.path.abspath(root)
    if root.startswith("-"):
        return False

    top = _git_toplevel(root)
    if top is None:
        return _looks_like_a_repository(root)
    try:
        # Inode identity, not string equality: the two paths can differ by
        # symlink or by Windows short-vs-long form and still be one directory.
        return os.path.samefile(top, root)
    except OSError:
        return os.path.realpath(top) == os.path.realpath(root)


def running_from_checkout(root: str) -> bool:
    """Is *root* the checkout the ``kiro_crew`` package THIS process runs from?

    The signal :func:`is_git_worktree` cannot supply: that *root* is a real
    working tree says nothing about whether the running code came from it. A
    release install (a wheel in its own venv) whose ``KIROCREW_PROJECT_DIR``
    resolves onto a checkout — the CWD-walking project detection does exactly
    this when the gateway is launched from inside a clone — passes the path
    probe while its bytes are owned by the release feed. Classifying it as a
    git install misreports the version drift, and the apply endpoint would then
    ``git pull`` + ``pip install -e`` that clone, silently replacing the
    release install with whatever the clone contains.

    Delegates to :func:`kiro_crew._bootstrap._source_checkout_root`, which
    resolves where the imported package actually loads from: the repo root for
    an editable install or a ``PYTHONPATH=src`` dev run — the layouts whose
    bytes ``git pull`` genuinely updates — and ``None`` for anything resolving
    through an installed tree (a wheel's ``site-packages``, including a venv
    nested under an unrelated repository such as a dotfiles home). *root* must
    then be that same directory: a DIFFERENT checkout is exactly the
    misclassification this refuses.
    """
    if not root or "\x00" in root:
        return False
    checkout = _source_checkout_root()
    if checkout is None:
        return False
    target = os.path.abspath(root)
    try:
        # Inode identity, not string equality — same reasoning as the anchor
        # comparison in is_git_worktree.
        return os.path.samefile(str(checkout), target)
    except OSError:
        return os.path.realpath(str(checkout)) == os.path.realpath(target)


@dataclass(frozen=True)
class UpdateCapability:
    """What this install can do about updates, and who owns doing it."""

    supported: bool
    managed_by: str
    mode: str
    can_download: bool
    can_apply: bool
    requires_restart: bool
    unavailable_reason: str | None = None
    remediation: dict[str, str] | None = None

    @property
    def defers(self) -> bool:
        """Does another surface own this install's verdict as well as its bytes?"""
        return self.unavailable_reason is not None

    def for_channel(self, channel: str) -> "UpdateCapability":
        """This capability with any channel-dependent remediation re-pinned.

        The wheel remediation embeds the channel (``cli.sh … --channel <lane>``),
        and it is composed when the capability is DERIVED. A caller that reports a
        channel it read separately — the feed check does, because the feed URL needs
        it — would otherwise publish a channel and a command that disagree: the
        panel says "insider" while the command it offers moves the install to
        "stable". Copy-pasting it would silently change the user's release lane,
        which is worse than showing nothing.

        Only the wheel command carries a channel. A git checkout follows its remote
        and an externally managed install is told to use its own updater, so both
        are returned untouched.
        """
        if self.managed_by != MANAGED_BY_KIROCREW or self.remediation is None:
            return self
        if self.remediation.get("kind") != "command":
            return self

        # Function-local for the same cycle reason as the import in the factory.
        from kiro_crew.platform.update_layout import wheel_update_command

        repinned = dict(self.remediation)
        repinned["command"] = wheel_update_command(channel)
        return replace(self, remediation=repinned)

    def to_dict(self) -> dict[str, Any]:
        return {
            "supported": self.supported,
            "managed_by": self.managed_by,
            "mode": self.mode,
            "can_download": self.can_download,
            "can_apply": self.can_apply,
            "requires_restart": self.requires_restart,
            "unavailable_reason": self.unavailable_reason,
            "remediation": dict(self.remediation) if self.remediation is not None else None,
        }


def _command_remediation(message: str, command: str) -> dict[str, str]:
    return {"kind": "command", "message": message, "command": command}


def derive_capability(
    *,
    install_root: str | None = None,
    dist: str | None = None,
) -> UpdateCapability:
    """Derive the capability contract for this install.

    *install_root* defaults to ``KIROCREW_PROJECT_DIR`` and *dist* to the value
    stamped into the package tree at packaging time.

    An externally managed stamp wins over a git working tree. A packaged app
    that happens to be pointed at a checkout is still updated by its own
    updater, and letting the checkout win would put the desktop lane back on the
    CLI release stream.

    Shapes are otherwise matched by EXCLUSION: wheels published before the
    distribution stamp existed carry no value and report the ``source`` default,
    so an ``== "wheel"`` allowlist would skip every already-released CLI install.
    """
    if dist is None:
        dist = distribution()
    if install_root is None:
        install_root = os.environ.get("KIROCREW_PROJECT_DIR", "")

    if dist in _ELECTRON_DISTRIBUTIONS:
        # ``can_apply`` answers for the process serving this contract, and that is
        # the gateway, whose apply endpoint is git-only. The Electron updater does
        # apply in-app, and the desktop About panel renders from its own IPC
        # channel; claiming the capability here would put a button in front of an
        # endpoint that refuses it.
        return UpdateCapability(
            supported=True,
            managed_by=MANAGED_BY_ELECTRON,
            mode=MODE_CONSENT,
            can_download=False,
            can_apply=False,
            requires_restart=True,
            unavailable_reason=UNAVAILABLE_MANAGED_BY_APP,
        )

    if dist in _CONTAINER_DISTRIBUTIONS:
        # supported, not unsupported: the UI is still allowed to show version
        # drift even though nothing here can act on it.
        return UpdateCapability(
            supported=True,
            managed_by=MANAGED_BY_CONTAINER,
            mode=MODE_NOTIFY,
            can_download=False,
            can_apply=False,
            requires_restart=True,
            unavailable_reason=UNAVAILABLE_MANAGED_BY_IMAGE,
            remediation={
                "kind": "image_pull",
                "message": EXTERNALLY_MANAGED_MESSAGES[UNAVAILABLE_MANAGED_BY_IMAGE],
                "command": "",
            },
        )

    if is_git_worktree(install_root) and running_from_checkout(install_root):
        # BOTH halves are required for the git lane. The worktree probe answers
        # "is this path a checkout"; provenance answers "is it THIS install's
        # checkout". A release install pointed at someone's clone (the CWD-derived
        # project dir does this) passes the first and must not take this branch:
        # its updates come from the release feed below, and the apply endpoint
        # would otherwise replace the install with the clone's contents.
        #
        # can_apply is true because ``POST /api/update`` genuinely applies on a
        # checkout: git fetch + reset + rebuild + restart. It answers only
        # "can the running process apply this", which is the question an
        # implementer reads it for.
        return UpdateCapability(
            supported=True,
            managed_by=MANAGED_BY_GIT,
            mode=MODE_NOTIFY,
            can_download=True,
            can_apply=True,
            requires_restart=True,
            remediation=_command_remediation(
                "Update this checkout from a terminal.", "kirocrew update"
            ),
        )

    # Function-local: update_layout imports this module for its own shape
    # detection, so importing it at module scope would close a cycle.
    from kiro_crew.platform.update_layout import release_channel, wheel_update_command

    return UpdateCapability(
        supported=True,
        managed_by=MANAGED_BY_KIROCREW,
        mode=MODE_NOTIFY,
        can_download=True,
        # Applying in-process would overwrite the bytes this gateway is
        # executing. The wheel engine that makes it survivable is a later phase.
        can_apply=False,
        requires_restart=True,
        remediation=_command_remediation(
            "Re-run the installer to upgrade.",
            wheel_update_command(release_channel()),
        ),
    )


__all__ = [
    "CHECK_CHECKING",
    "CHECK_DEFERRED",
    "CHECK_FAILED",
    "CHECK_SUCCEEDED",
    "CHECK_UNCHECKED",
    "ERR_FEED_MALFORMED",
    "ERR_FEED_UNREACHABLE",
    "ERR_GIT_FETCH_FAILED",
    "ERR_GIT_READ_FAILED",
    "ERR_UNKNOWN",
    "ERR_VERSION_UNPARSEABLE",
    "EXTERNALLY_MANAGED_MESSAGES",
    "EXTERNALLY_MANAGED_STAMPS",
    "MANAGED_BY_CONTAINER",
    "MANAGED_BY_ELECTRON",
    "MANAGED_BY_GIT",
    "MANAGED_BY_KIROCREW",
    "MANAGED_BY_NONE",
    "MODE_AUTO",
    "MODE_CONSENT",
    "MODE_NONE",
    "MODE_NOTIFY",
    "UNAVAILABLE_MANAGED_BY_APP",
    "UNAVAILABLE_MANAGED_BY_IMAGE",
    "UpdateCapability",
    "derive_capability",
    "is_git_worktree",
    "running_from_checkout",
]
