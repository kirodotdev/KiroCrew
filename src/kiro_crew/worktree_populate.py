"""Fill a freshly added linked worktree without reading the repository's config.

``git worktree add <dest> <ref>`` checks the tree out under the repository's own
``.git/config``, so a ``filter.<name>.smudge`` (or ``.process``) driver written there
runs during the checkout. A probe that lists the config's filter keys in one git
spawn and a checkout in the next leaves a gap in which a driver can be planted. This
module closes the gap by making the populate step independent of that config.

The caller registers the worktree with ``git worktree add --no-checkout`` (it reads
the config but checks no file out, so no driver can run), then calls
:func:`populate_worktree`, which:

1. asks git for the new worktree's own git dir and the repository's common dir;
2. reads six non-executing settings (:data:`_ALLOWED`) from the effective config,
   validates each value against a closed set, and drops anything else;
3. writes a throwaway git dir under the crew scratch root whose ``config`` holds
   only those values, and whose ``objects/info/alternates`` names the repository's
   object store;
4. runs ``git read-tree -u --reset <oid>`` against that throwaway dir, with the linked
   worktree's index as ``GIT_INDEX_FILE`` and the worktree as ``GIT_WORK_TREE``, with
   system and global config switched off.

The repository's ``.git/config`` is never an input to the checkout that writes the
files, and neither are ``$GIT_DIR/info/attributes`` or ``core.attributesFile``; only
the ``.gitattributes`` files of the tree itself apply. A filter named there has no
driver, so git leaves the content as it is stored.

BEHAVIOUR THAT FOLLOWS, each pinned by ``test_worktree_populate.py``:

* A Git LFS file lands as its pointer text, because the LFS filter is a driver and no
  driver is defined. A caller that needs LFS content must fetch it afterwards.
* Line endings follow ``core.autocrlf`` / ``core.eol`` as the repository sets them.
* Symlinks follow ``core.symlinks``, and case handling follows ``core.ignorecase``.
* Sparse-checkout is kept: ``core.sparseCheckout``, ``core.sparseCheckoutCone`` and the
  pattern file git copied into the new worktree's ``info/`` carry over, so the tree is as
  sparse as ``worktree add`` made it. An unreadable pattern file drops sparse and the
  tree is a full checkout.
* Submodules stay unpopulated, as they do after ``worktree add``.
* A partial clone is refused: its missing blobs would be fetched during the checkout, and
  the throwaway git dir names no promisor remote to fetch from.

``run_git`` is the caller's sandboxed runner (``dashboard.handlers.worktree._run_git``
has the signature this module needs). It is a parameter so this module does not import
the handler that imports it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping

#: ``run_git(args, cwd, env=...)`` as ``dashboard.handlers.worktree._run_git`` has it.
RunGit = Callable[..., "subprocess.CompletedProcess[str]"]

_OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

_TRUE = frozenset({"true", "yes", "on", "1"})
_FALSE = frozenset({"false", "no", "off", "0"})

#: The settings the populate keeps, each with the values it accepts. None of them
#: names a program. ``core.autocrlf`` and ``core.eol`` decide line endings;
#: ``core.symlinks`` and ``core.ignorecase`` decide how the filesystem is treated, and
#: a Windows checkout needs them to behave as the clone does.
_ALLOWED: dict[str, frozenset[str]] = {
    "core.symlinks": _TRUE | _FALSE,
    "core.ignorecase": _TRUE | _FALSE,
    "core.autocrlf": _TRUE | _FALSE | {"input"},
    "core.eol": frozenset({"lf", "crlf", "native"}),
    "core.sparsecheckout": _TRUE | _FALSE,
    "core.sparsecheckoutcone": _TRUE | _FALSE,
}

#: The config keys the populate reads: the allowlisted settings, and the two spellings of a
#: promisor remote that :func:`promisor_in_force` judges.
_QUERY_PATTERN = (
    r"^(core\.(symlinks|ignorecase|autocrlf|eol|sparsecheckout|sparsecheckoutcone)"
    r"|extensions\.partialclone|remote\..*\.promisor)$"
)

#: The most bytes of the sparse-checkout pattern file that are carried over.
_MAX_SPARSE_BYTES = 1024 * 1024

#: Environment variables that would aim git at another repository or object store, or
#: add config from the environment. Dropped from the populate's environment.
_REDIRECT_VARS = frozenset(
    {
        "GIT_DIR",
        "GIT_COMMON_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_PREFIX",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_ATTR_SOURCE",
    }
)


#: git's spellings of a false boolean, lower-cased, an empty value among them. git reads
#: every other value as true (any non-zero integer included), so anything else counts.
_GIT_FALSE = frozenset({"false", "no", "off", "0", ""})


def promisor_in_force(config_list: str) -> bool:
    """Whether ``git config --list -z`` output names a promisor remote in force.

    git records a promisor remote two ways: ``extensions.partialClone`` (a remote name;
    set at all means partial) and ``remote.<name>.promisor`` (a boolean -- a hydrated
    clone can keep it as ``false``). Each entry of the listing is ``key`` and ``value``
    joined by a newline and ended by a NUL, and a key with no value is the bare key, so a
    value that holds a newline cannot pass for a key. Keys are compared exactly, after
    lower-casing (git's section and variable names are case-insensitive).

    Read by value, and closed: a ``remote.<name>.promisor`` counts unless it holds one of
    git's false spellings, so a ``false`` left behind by a hydrated clone is no partial
    clone while ``2`` is one. The bare key is true and an empty value is false. A rare
    false spelling outside the set (``00``) reads as in force and is refused, the safe
    side of the mistake.
    """
    for entry in config_list.split("\0"):
        key, sep, value = entry.partition("\n")
        key = key.lower()
        if key == "extensions.partialclone":
            return True
        if key.startswith("remote.") and key.endswith(".promisor"):
            if not sep or value.strip().lower() not in _GIT_FALSE:
                return True
    return False


def _failed(args: list[str], message: str) -> "subprocess.CompletedProcess[str]":
    """The result for a step this module refused before, or instead of, running git."""
    return subprocess.CompletedProcess(["git", *args], 128, "", f"fatal: {message}")


def _canonical(key: str, value: str) -> str | None:
    """*value* for *key* in the form written to the throwaway config, or ``None``.

    ``None`` when the value is not one the allowlist names, so a setting that is
    anything else (or carries a newline) is dropped and the git default applies.
    """
    folded = value.strip().lower()
    if folded not in _ALLOWED[key]:
        return None
    if key not in ("core.autocrlf", "core.eol"):
        return "true" if folded in _TRUE else "false"
    if key == "core.autocrlf":
        return "input" if folded == "input" else ("true" if folded in _TRUE else "false")
    return folded


def allowed_settings(listing: str) -> dict[str, str]:
    """The allowlisted settings in ``git config --null --get-regexp`` output.

    Each entry is ``key``, a newline, the value, and a NUL. The last assignment of a
    key wins, as it does in git. A value outside the closed set for its key is dropped.
    """
    found: dict[str, str] = {}
    for entry in listing.split("\0"):
        key, _, value = entry.partition("\n")
        key = key.lower()
        if key not in _ALLOWED:
            continue
        canonical = _canonical(key, value)
        if canonical is None:
            found.pop(key, None)
        else:
            found[key] = canonical
    return found


def _config_text(settings: Mapping[str, str], *, sha256: bool) -> str:
    """The throwaway git dir's ``config``: its object format and the allowlisted values."""
    lines = ["[core]", f"\trepositoryformatversion = {1 if sha256 else 0}"]
    lines += [f"\t{key.split('.', 1)[1]} = {value}" for key, value in sorted(settings.items())]
    if sha256:
        lines += ["[extensions]", "\tobjectformat = sha256"]
    return "\n".join(lines) + "\n"


def _populate_env(scratch: str, dest: str, index: str) -> dict[str, str]:
    """The environment for the one ``read-tree``: only the throwaway dir is a config source."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in _REDIRECT_VARS and not key.upper().startswith("GIT_CONFIG")
    }
    env.update(
        GIT_DIR=scratch,
        GIT_WORK_TREE=dest,
        GIT_INDEX_FILE=index,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
    )
    return env


def _sparse_patterns(git_dir: str) -> str | None:
    """The sparse-checkout pattern text git wrote for the new worktree, or ``None``.

    ``None`` when the file is missing, over :data:`_MAX_SPARSE_BYTES`, refused, or
    unreadable, and the caller then populates a full tree rather than a sparse one with no
    patterns. The read runs in the gateway, and the file sits in a directory a sandboxed
    agent can write, so it goes through ``safe_read_prefix``: a link planted in its place
    that resolves to a sensitive path is refused, and the open does not follow a final link.
    """
    from kiro_crew.hooks import safe_read_prefix

    raw = safe_read_prefix(os.path.join(git_dir, "info", "sparse-checkout"), _MAX_SPARSE_BYTES + 1)
    if raw is None or len(raw) > _MAX_SPARSE_BYTES:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _write_scratch_git_dir(
    scratch: str,
    objects: str,
    settings: Mapping[str, str],
    *,
    sha256: bool,
    sparse: str | None,
) -> None:
    """Lay out the throwaway git dir: no refs, an alternates file, a minimal config."""
    os.makedirs(os.path.join(scratch, "objects", "info"))
    os.makedirs(os.path.join(scratch, "refs"))
    if sparse is not None:
        os.makedirs(os.path.join(scratch, "info"))
        with open(
            os.path.join(scratch, "info", "sparse-checkout"), "w", encoding="utf-8", newline="\n"
        ) as h:
            h.write(sparse)
    with open(os.path.join(scratch, "HEAD"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write("ref: refs/heads/kirocrew-populate\n")
    with open(os.path.join(scratch, "config"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(_config_text(settings, sha256=sha256))
    with open(
        os.path.join(scratch, "objects", "info", "alternates"), "w", encoding="utf-8", newline="\n"
    ) as h:
        h.write(objects + "\n")


def populate_worktree(run_git: RunGit, dest: str, oid: str) -> "subprocess.CompletedProcess[str]":
    """Check *oid* out into *dest*, a worktree added with ``--no-checkout``.

    Returns the ``CompletedProcess`` of the step that failed, or of the ``read-tree``
    that succeeded. Everything *run_git* raises (a missing sandbox, a timeout) passes
    through, and the throwaway git dir is removed on every path. The caller unwinds
    the worktree on a failure, as it does for a failed ``worktree add``.
    """
    if not _OID_RE.fullmatch(oid):
        return _failed(["read-tree"], f"not an object id: {oid!r}")
    where = ["rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"]
    located = run_git(where, dest)
    if located.returncode != 0:
        return located
    # Split on the newline git ends each answer with and nothing else: a directory name may
    # hold a carriage return or another line separator, which `splitlines` would cut at.
    lines = located.stdout.removesuffix("\n").split("\n")
    if len(lines) != 2 or not all(os.path.isabs(line) for line in lines):
        return _failed(where, "cannot locate the new worktree's git directory")
    git_dir, common_dir = lines
    objects = os.path.join(common_dir, "objects")
    # `--get-regexp` exits 1 when nothing matches, which is a repository with none set.
    listed = run_git(["config", "--null", "--get-regexp", _QUERY_PATTERN], dest)
    if listed.returncode not in (0, 1):
        return listed
    if promisor_in_force(listed.stdout):
        return _failed(
            ["read-tree"],
            "this repository is a partial clone (a promisor remote is configured), so the "
            "files would be fetched during the checkout; create the worktree manually",
        )
    settings = allowed_settings(listed.stdout)
    sparse = _sparse_patterns(git_dir) if settings.get("core.sparsecheckout") == "true" else None
    if sparse is None:
        settings.pop("core.sparsecheckout", None)
        settings.pop("core.sparsecheckoutcone", None)

    from kiro_crew import agent_scratch

    root = agent_scratch.scratch_root()
    os.makedirs(root, exist_ok=True)
    scratch = tempfile.mkdtemp(prefix="kirocrew-populate-", dir=str(root))
    try:
        _write_scratch_git_dir(scratch, objects, settings, sha256=len(oid) == 64, sparse=sparse)
        env = _populate_env(scratch, dest, os.path.join(git_dir, "index"))
        return run_git(["read-tree", "-u", "--reset", oid], dest, env=env, private_dir=scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
