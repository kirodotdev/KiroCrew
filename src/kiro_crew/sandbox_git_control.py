"""The git control files of the gateway's own checkout, write-protected from the sandbox.

In a source-checkout install the agent's workspace is the install checkout
(``KIROCREW_PROJECT_DIR``), and the gateway runs ``git`` there itself, outside any
sandbox (the update check, Dev Fleet). Git reads its control files on every run:
``config`` can name programs git executes (``core.fsmonitor``, ``core.sshCommand``, a
filter or diff driver), ``hooks/`` holds programs git runs, ``info/attributes`` picks
the drivers, and a linked worktree's ``.git`` file and ``commondir`` say which of those
files apply. A sandboxed agent that can write any of them chooses what the gateway
executes next, with the gateway's identity.

So, for each such checkout, the sandbox:

* PINS the git directory (and, for a linked worktree, every directory from the common
  directory down to the worktree's own git directory) with a read-write bind over
  itself. A mount point cannot be renamed or removed, so the agent cannot move ``.git``
  aside and put a fresh one with its own ``config`` in its place. The checkout root and
  every directory above it that the agent could rename (its parent is writable) are
  pinned the same way, for the same reason one level up. Everything else in
  the directory -- objects, refs, the index, ``HEAD``, logs -- stays writable, so
  ``git commit``, ``git branch`` and ``git status`` keep working inside the sandbox.
* SEALS the control files read-only: ``config``, ``config.worktree``, ``hooks/``,
  ``info/``, ``commondir``, a linked worktree's ``.git`` file, every file an
  ``include.path`` / ``includeIf.*.path`` in those configs names, and the directory a
  ``core.hooksPath`` names.

A seal needs an object to cover, and a control file the agent could CREATE is as
dangerous as one it could edit, so missing ones are created first: ``hooks/`` and
``info/`` empty, and ``config.worktree`` empty (git ignores it unless
``extensions.worktreeConfig`` is on, and reads an empty one as nothing).

ONE GAP REMAINS on Linux: a plain checkout has no ``.git/commondir``, and git reads
that file in EVERY git directory, taking ``config`` and ``hooks/`` from the directory
it names. Seatbelt's path rules deny creating it; a bind mount (Linux) needs an existing
object to cover, and creating a stand-in is
not an option -- an empty one makes git fail, and libgit2 refuses to open a main
repository that has one at all -- so on Linux a sandboxed shell can still create it
inside the writable ``.git``. The gateway's own git callers must therefore not trust the
checkout's git directory on their own; this layer narrows what an agent can reach,
it does not replace that.

A ``.git`` that is a symlink is followed and its target sealed, with a warning: the
link itself lives in the writable checkout root and cannot be pinned.

The workspace-root ``.gitattributes`` is NOT sealed: it is a tracked file agents
legitimately edit, and an attribute can only select a driver that a config file
defines -- every repository config is sealed here.

Only the gateway's own checkouts get this, and the file-tool write gate refuses the
same files there (:func:`git_control_write`). Any other repository an agent works in
keeps a writable ``.git/config``. In the gateway's checkouts, git commands that write
it fail inside the sandbox: ``git push -u``, ``git remote add``, a branch that tracks
a remote, ``git config``, ``pre-commit install``. The shipped dev skills use
``--no-track`` and a plain ``git push`` for that reason.

A seal holds the object it covered at spawn. Host git rewrites ``config`` by writing
``config.lock`` and renaming it over, so after a gateway-side ``git config`` the name
reaches a fresh, unsealed file in a namespace that was already running; the next spawn
seals it again.

On Linux every pinned directory is its own mount, so ``rename(2)`` from one pinned
directory into another (for example from the parent workspace into the checkout)
fails with ``EXDEV``; ``mv`` falls back to copying.
"""

from __future__ import annotations

import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path

import kiro_crew as _kiro_crew_pkg

logger = logging.getLogger("kiro_crew.sandbox")

#: A config bigger than this is not parsed for includes; its own seal still holds.
_MAX_CONFIG_BYTES = 1 << 20


@dataclass(frozen=True)
class GitControlLayout:
    """What the sandbox pins and seals for the gateway's checkouts.

    ``pins`` are directories bound read-write over themselves, outermost first.
    ``readonly`` are files and directories sealed read-only. ``guards`` are the
    directories whose rename a path-based backend (Seatbelt) must deny itself: the pins
    and every ancestor of each checkout root.
    """

    pins: tuple[str, ...] = ()
    readonly: tuple[str, ...] = ()
    guards: tuple[str, ...] = ()


def _own_source_checkout() -> str | None:
    """The source checkout this process imports from, or ``None`` when packaged."""
    package_dir = Path(_kiro_crew_pkg.__file__).resolve().parent
    src = package_dir.parent
    if src.name != "src":
        return None
    return str(src.parent)


def gateway_checkout_roots() -> tuple[str, ...]:
    """The checkouts whose git files the gateway's own unsandboxed git reads."""
    roots = [os.environ.get("KIROCREW_PROJECT_DIR", ""), _own_source_checkout() or ""]
    resolved: list[str] = []
    for root in roots:
        if not root:
            continue
        try:
            canonical = os.path.realpath(root)
        except (OSError, ValueError):
            continue
        if os.path.lexists(os.path.join(canonical, ".git")) and canonical not in resolved:
            resolved.append(canonical)
    return tuple(resolved)


def _read_small(path: str) -> str | None:
    """*path*'s text when it is a regular file of at most :data:`_MAX_CONFIG_BYTES`.

    Opened non-blocking and judged on the DESCRIPTOR, so a FIFO swapped in after a
    ``stat`` cannot hang the write gate or spawn prep. A leading UTF-8 BOM is dropped,
    as git drops it.
    """
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except (OSError, ValueError):
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_CONFIG_BYTES:
            return None
        data = os.read(fd, _MAX_CONFIG_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    return data.decode("utf-8-sig", errors="replace")


def _expand(value: str, base_dir: str) -> str:
    path = os.path.expanduser(value)
    if not os.path.isabs(path):
        path = os.path.join(base_dir, path)
    return os.path.normpath(path)


#: The keys whose values name a control file or directory.
_NAMED_KEYS = r"^(include|includeif\..*)\.path$|^core\.hookspath$"
#: How many config files one checkout's include walk reads at most.
_MAX_CONFIG_FILES = 64
#: How long git may take to list them; a slower read seals what it has.
_GIT_CONFIG_TIMEOUT_S = 10


def _config_closure(
    configs: list[str], worktree_root: str, gitdir: str, common: str
) -> tuple[list[str], list[str]]:
    """Every include target and ``core.hooksPath`` the configs name, as git reads them.

    Git itself parses the files (``git config --file <f> --includes --show-origin``),
    so quoting, continuations, comments and nested includes resolve exactly as the
    gateway's git resolves them. ``GIT_DIR`` and ``GIT_COMMON_DIR`` are set to the
    resolved directories, so ``includeIf`` conditions see this checkout and no
    ``commondir`` file is consulted; system and global config are not read. Listing
    config runs no program a config names. Returns ``(included files, hooks paths)``
    as spelled, absent ones included. Empty when git is missing or fails.
    """
    import subprocess

    from kiro_crew import platform_compat

    # From fixed system directories, never ``PATH``: this runs as the gateway, and a
    # ``PATH`` entry such as the checkout's own venv ``bin`` is agent-writable.
    git = platform_compat.trusted_git_bin()
    if not git:
        logger.warning("sandbox: no trusted git; include targets of %s are not sealed", gitdir)
        return [], []
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_DIR=gitdir,
        GIT_COMMON_DIR=common,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
    )
    included: list[str] = []
    hooks_paths: list[str] = []
    # Every existing include is also listed ON ITS OWN, whatever its condition says
    # now: a condition the agent can turn on later (``onbranch:`` follows HEAD, which
    # stays writable) would otherwise hide the includes nested inside it.
    queue = list(configs)
    visited: set[str] = set()
    while queue and len(visited) < _MAX_CONFIG_FILES:
        config = queue.pop(0)
        if config in visited or not os.path.isfile(config):
            continue
        visited.add(config)
        # ``--path``: git expands ``~/`` and ``%(prefix)/`` itself; only an
        # origin-relative value is left for ``_expand`` to join.
        argv = [git, "config", "--file", config, "--includes", "--path", "--show-origin", "-z"]
        try:
            proc = subprocess.run(
                [*argv, "--get-regexp", _NAMED_KEYS],
                env=env,
                cwd=worktree_root,
                capture_output=True,
                timeout=_GIT_CONFIG_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            logger.warning("sandbox: could not list the includes of %s", config)
            continue
        fields = proc.stdout.decode("utf-8", errors="surrogateescape").split("\0")
        for origin, entry in zip(fields[0::2], fields[1::2]):
            key, _, value = entry.partition("\n")
            if not value or not origin.startswith("file:"):
                continue
            if key == "core.hookspath":
                hooks_paths.append(_expand(value, worktree_root))
            else:
                target = _expand(value, os.path.dirname(origin[len("file:") :]))
                included.append(target)
                queue.append(target)
    return included, hooks_paths


def _resolve_gitdir(root: str) -> tuple[str, str, str | None] | None:
    """``(gitdir, commondir, dotgit file)`` for the checkout at *root*, canonical.

    The third element is the ``.git`` FILE of a linked worktree, or ``None`` when
    ``.git`` is a directory. ``None`` overall when *root* holds no readable git layout.
    """
    dotgit = os.path.join(root, ".git")
    try:
        info = os.lstat(dotgit)
    except OSError:
        return None
    dotgit_file: str | None = None
    if stat.S_ISREG(info.st_mode):
        text = _read_small(dotgit)
        if not text or not text.startswith("gitdir:"):
            return None
        gitdir = _expand(text[len("gitdir:") :].strip(), root)
        dotgit_file = dotgit
    else:
        # A real directory, or a link to one: the link itself cannot be pinned, but its
        # target can, and the target is what git reads.
        if stat.S_ISLNK(info.st_mode):
            logger.warning(
                "sandbox: %s is a symlink; its target is sealed, but a sandboxed agent "
                "can replace the link itself",
                dotgit,
            )
        gitdir = dotgit
    gitdir = os.path.realpath(gitdir)
    if not os.path.isdir(gitdir):
        return None
    common = gitdir
    pointer = os.path.join(gitdir, "commondir")
    if dotgit_file is None:
        # A plain checkout's git directory carries no ``commondir``; one found there
        # may be the agent's (see the module docstring) and is never followed, so no
        # config it points at is parsed and nothing it names is created.
        if os.path.lexists(pointer):
            logger.warning("sandbox: %s is unexpected in a plain checkout; not followed", pointer)
        return gitdir, common, dotgit_file
    text = _read_small(pointer)
    if text is not None and text.strip():
        common = os.path.realpath(_expand(text.strip(), gitdir))
        if not os.path.isdir(common):
            return None
    return gitdir, common, dotgit_file


def _ensure_dir(path: str) -> bool:
    """Create *path* as an empty directory when absent; whether a real one is there."""
    try:
        os.mkdir(path, 0o755)
    except FileExistsError:
        pass
    except OSError:
        logger.warning("sandbox: could not create %s to seal it read-only", path)
        return False
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode)


def _ensure_file(path: str, content: bytes = b"") -> bool:
    """Create *path* holding *content* when absent; whether a regular file is there."""
    try:
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o644,
        )
        try:
            os.write(fd, content)
        finally:
            os.close(fd)
    except FileExistsError:
        pass
    except OSError:
        logger.warning("sandbox: could not create %s to seal it read-only", path)
        return False
    return os.path.isfile(path)


def _ancestors(path: str) -> list[str]:
    """*path* and every directory above it, below the filesystem root, innermost first."""
    out: list[str] = []
    current = os.path.normpath(path)
    while True:
        parent = os.path.dirname(current)
        if parent == current:
            break
        out.append(current)
        current = parent
    return out


def _renamable(paths: list[str]) -> list[str]:
    """The entries of *paths* this process could rename: their parent is writable.

    A bind mount follows the directory, not its name, so renaming a directory above a
    seal and building a look-alike at the old name defeats the seal for every reader
    that goes by the name -- the gateway's own ``git`` does. A mount point cannot be
    renamed, so each such directory is pinned too. A directory whose parent this user
    cannot write cannot be renamed by the agent either and needs no pin.
    """
    return [path for path in paths if os.access(os.path.dirname(path), os.W_OK)]


def _depth(path: str) -> int:
    return len([part for part in path.split(os.sep) if part])


def _seal_named(
    path: str,
    *,
    directory: bool,
    create: bool,
    pins: list[str],
    readonly: list[str],
    guards: list[str],
) -> None:
    """Seal a control file or directory at the name git reads.

    *create* is for the names this layout derives itself (``hooks/`` and ``info/`` in
    the resolved git directory): an absent one is created empty first, so the agent
    cannot create it. A name a config spells is NEVER created -- the gateway would be
    making files at a path a config chose -- so an absent one stays listed for
    Seatbelt's path rule and is logged; on Linux the agent can still create it. The
    canonical target is sealed, and every directory above both spellings that the
    agent could rename is pinned, so the name keeps reaching the seal. A symlink on the
    way is followed and logged: the link itself cannot be pinned.
    """
    if not os.path.lexists(path):
        if not create:
            logger.warning(
                "sandbox: %s is named by git config but absent; on Linux a "
                "sandboxed agent can create it",
                path,
            )
            readonly.append(os.path.normpath(path))
            return
        if not (_ensure_dir(path) if directory else _ensure_file(path)):
            return
    canonical = os.path.realpath(path)
    if canonical != os.path.normpath(path):
        logger.warning(
            "sandbox: %s reaches %s through a symlink; the target is sealed, but a "
            "sandboxed agent can replace the link itself",
            path,
            canonical,
        )
    if directory != os.path.isdir(canonical):
        logger.warning("sandbox: %s is not the kind git expects; not sealed", path)
        return
    readonly.append(canonical)
    for spelling in dict.fromkeys((os.path.dirname(canonical), os.path.dirname(path))):
        existing = [p for p in _ancestors(spelling) if os.path.isdir(p)]
        pins.extend(_renamable(existing))
        guards.extend(existing)


def git_control_layout_for(roots: tuple[str, ...]) -> GitControlLayout:
    """Pin and seal the git control files of each checkout in *roots*.

    Reads the host and may create ``hooks/``, ``info/`` and ``config.worktree`` (see the
    module docstring). Never raises: a checkout whose layout cannot be read contributes
    nothing and is logged.
    """
    pins: list[str] = []
    readonly: list[str] = []
    guards: list[str] = []
    for root in roots:
        try:
            layout = _resolve_gitdir(root)
        except (OSError, ValueError):
            layout = None
        if layout is None:
            logger.warning(
                "sandbox: %s has no readable git layout; its git files stay writable",
                root,
            )
            continue
        gitdir, common, dotgit_file = layout
        # Pins, outermost first: the common directory, then each directory below it on
        # the way to a linked worktree's own git directory.
        chain = [common]
        if gitdir != common and gitdir.startswith(common + os.sep):
            rel = os.path.relpath(gitdir, common).split(os.sep)
            for i in range(1, len(rel) + 1):
                chain.append(os.path.join(common, *rel[:i]))
        elif gitdir != common:
            chain.append(gitdir)
        pins.extend(chain)
        # The checkout root and the common directory are reached by name, so every
        # directory above them that the agent could rename is pinned as well.
        pins.extend(_renamable(_ancestors(root)))
        pins.extend(_renamable(_ancestors(os.path.dirname(common))))
        seal: dict[str, list[str]] = {"pins": pins, "readonly": readonly, "guards": guards}
        if dotgit_file is not None:
            _seal_named(dotgit_file, directory=False, create=False, **seal)
        # Sealed when present. An ABSENT one is not created: libgit2 refuses to open a
        # main repository that carries a ``commondir`` (measured), so a stand-in would
        # break the user's own tools. See the module docstring for what that leaves.
        # Its absence is the normal plain-checkout state: listed for Seatbelt, not logged.
        commondir = os.path.join(gitdir, "commondir")
        if os.path.lexists(commondir):
            _seal_named(commondir, directory=False, create=False, **seal)
        else:
            readonly.append(commondir)
        for name in ("hooks", "info"):
            _seal_named(os.path.join(common, name), directory=True, create=True, **seal)
        configs = [os.path.join(common, "config")]
        configs += [os.path.join(d, "config.worktree") for d in dict.fromkeys((common, gitdir))]
        for config in configs[1:]:
            _ensure_file(config)
        # Through ``_seal_named`` so a symlinked config's target and its renamable
        # parents are sealed and pinned, not only the name.
        for config in configs:
            if os.path.isfile(config):
                _seal_named(config, directory=False, create=False, **seal)
        included, hooks_paths = _config_closure(configs, root, gitdir, common)
        for target in included:
            _seal_named(target, directory=False, create=False, **seal)
        for hooks in hooks_paths:
            _seal_named(hooks, directory=True, create=False, **seal)
        guards.extend(chain)
        guards.extend(_ancestors(root))
        guards.extend(_ancestors(os.path.dirname(common)))
    # An absent ``commondir`` or config-named target stays listed: Seatbelt's path rule
    # denies CREATING it, and the namespace launcher skips a seal whose target is absent.
    # Outermost first: a pin bound after one inside it would cover the inner pin.
    ordered_pins = sorted(dict.fromkeys(pins), key=_depth)
    return GitControlLayout(
        pins=tuple(ordered_pins),
        readonly=tuple(dict.fromkeys(readonly)),
        guards=tuple(dict.fromkeys(guards)),
    )


def resolved_git_control_layout() -> GitControlLayout:
    """:func:`git_control_layout_for` the gateway's own checkouts. Never raises."""
    try:
        return git_control_layout_for(gateway_checkout_roots())
    except Exception:
        logger.warning("sandbox: could not resolve the checkout's git files", exc_info=True)
        return GitControlLayout()


# --------------------------------------------------------------------------- #
# The file-tool gate: the gateway's own checkouts.
# --------------------------------------------------------------------------- #

#: Entries directly inside a git directory that name code git runs or files it trusts.
_GITDIR_CONTROL_LEAVES = frozenset({"config", "config.worktree", "commondir", "gitdir"})
#: Directories inside a git directory whose whole subtree is control data.
_GITDIR_CONTROL_TREES = frozenset({"hooks", "info"})


def _component(name: str) -> str:
    # Case-folded, with the trailing dots and spaces Windows drops from a name, so a
    # case variant on a case-insensitive filesystem matches the same entry.
    return name.rstrip(". ").casefold()


def _gitdir_rest_is_control(rest: list[str]) -> bool:
    """Whether *rest*, the components below a git directory, names a control file."""
    if not rest:
        return False
    head = rest[0]
    if head in _GITDIR_CONTROL_TREES:
        return True
    if len(rest) == 1:
        return head in _GITDIR_CONTROL_LEAVES
    if head == "worktrees" and len(rest) >= 3:
        return _gitdir_rest_is_control(rest[2:])
    if head == "modules":
        # A submodule's git directory sits at modules/<name>[/modules/<name>...]; its
        # name may itself contain separators, so every suffix is tried.
        return any(_gitdir_rest_is_control(rest[i:]) for i in range(2, len(rest)))
    return False


def _gateway_git_dirs() -> list[tuple[str, str]]:
    """``(checkout root, git directory)`` pairs of the gateway's checkouts, read-only.

    Each checkout contributes its own git directory and, for a linked worktree, the
    common one. Unlike :func:`git_control_layout_for` this creates nothing, so the
    file-tool gate can ask it on every write.
    """
    pairs: list[tuple[str, str]] = []
    for root in gateway_checkout_roots():
        try:
            layout = _resolve_gitdir(root)
        except (OSError, ValueError):
            continue
        if layout is None:
            continue
        gitdir, common, _dotgit_file = layout
        for directory in dict.fromkeys((gitdir, common)):
            pairs.append((root, directory))
    return pairs


def _is_gateway_git_control(cand: str, pairs: list[tuple[str, str]]) -> bool:
    folded = _component_path(cand)
    for root, directory in pairs:
        if folded == _component_path(os.path.join(root, ".git")):
            return True
        base = _component_path(directory)
        if folded == base or not folded.startswith(base + "/"):
            continue
        if _gitdir_rest_is_control(folded[len(base) + 1 :].split("/")):
            return True
    return False


def _component_path(path: str) -> str:
    return "/".join(_component(p) for p in path.replace("\\", "/").split("/") if p)


def git_control_write(path_str: str, base_dir: str | None = None) -> bool:
    """Whether a file-tool write to *path_str* would change a gateway checkout's git
    control file -- the same files the OS sandbox seals (:func:`git_control_layout_for`).

    Only the gateway's own checkouts: in any other repository the shell can write the
    same files, and a file-tool edit of ``.git/info/exclude`` there is ordinary work.
    Every candidate spelling (resolved, lexical, raw) is checked, so ``..`` and a
    symlink into the git directory are caught. A resolver stall raises
    ``PathResolutionStalled``, which the write gate turns into a refusal.
    """
    if not path_str:
        return False
    pairs = _gateway_git_dirs()
    if not pairs:
        return False
    # Call-time import: the security package imports this module at load.
    from kiro_crew.security.paths_targets import _candidate_forms

    return any(_is_gateway_git_control(c, pairs) for c in _candidate_forms(path_str, base_dir))
