"""Git operations for the Notes builtin: clone, attach, status, commit, sync.

Ported from the app's ``@md-notebook/core-git`` package, which was built on
isomorphic-git (a pure-JS git implementation). This version drives the real
``git`` binary instead, which removes two whole layers the JS needed:

* its ``localTransport`` module existed only because isomorphic-git's HTTP
  client cannot speak ``file://`` remotes — real git handles local remotes
  natively, so local and remote vaults follow one code path here;
* its auth plumbing wrapped a PAT into an ``onAuth`` callback — here a token is
  passed through ``GIT_CONFIG_*`` environment variables, which apply to a
  single invocation and are never written into the repository's config.

Credential handling: the token is sent as an ``Authorization`` header supplied
through the environment. It is never interpolated into a URL (which would
persist it in ``.git/config`` and leak it into any error message that echoes
the remote) and never passed as a command-line argument (which would expose it
in the process table).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Identity used for commits the app makes on the user's behalf.
DEFAULT_AUTHOR_NAME = "md-notebook"
DEFAULT_AUTHOR_EMAIL = "noreply@md-notebook.local"

#: Cap on the number of paths one commit names in an argv. Staging is per-path
#: (never a directory-wide `add -A`), so a first sweep over a large vault would
#: otherwise risk exceeding the OS argument limit. Only the AUTOSAVE truncates to
#: it — the remainder lands on the next tick, and nothing was pushed. A
#: user-initiated Sync REFUSES instead of committing a subset, because a rename is
#: two entries and pushing only its deletion half would look like a deleted note.
MAX_STAGED_PATHS = 500

# Local trash folder inside the vault, holding notes the user deleted from the
# app. Named the way Obsidian names it, and dotted for two reasons: the note
# walk prunes dotted directories, so a trashed note never reappears in the
# listing, and `git status --porcelain` still reports it — which is why every
# git read and write below filters it out explicitly. It must NEVER reach the
# remote: a deleted note pushed to the repo defeats the point of deleting it.
TRASH_DIR = ".trash"


def in_trash(path: str) -> bool:
    """True when a vault-relative path lies inside the local trash folder."""
    return TRASH_DIR in path.replace("\\", "/").split("/")


# Seconds before a git invocation is abandoned. Network operations (clone,
# fetch, push) get the longer budget. Overridable via environment for hosts
# where subprocess spawn and filesystem latency are slow or highly variable
# (e.g. shared Windows CI runners) — mirrors FE_GIT_TIMEOUT_SEC in the
# file_explorer app.
GIT_TIMEOUT_SEC = int(os.environ.get("MDNB_GIT_TIMEOUT_SEC", 30))
GIT_NETWORK_TIMEOUT_SEC = int(os.environ.get("MDNB_GIT_NETWORK_TIMEOUT_SEC", 180))


class GitError(RuntimeError):
    """A git invocation failed. Carries the command's stderr tail."""


class AttachError(Exception):
    """A directory cannot serve as a vault. ``code`` is a UI-friendly token."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def _trim_repo_url(url: str) -> str:
    """Drop trailing separators and one trailing ``.git``, without a regex.

    The anchored quantifiers this replaces backtracked on a URL ending in a long
    run of slashes, which is attacker-shaped input for a polynomial-time match.
    """
    trimmed = url.rstrip("/\\")
    if trimmed.endswith(".git"):
        trimmed = trimmed[: -len(".git")]
    return trimmed.rstrip("/\\")


def repo_name(url: str) -> str:
    """Last path segment of a repo URL, minus any trailing ``.git``."""
    trimmed = _trim_repo_url(url)
    seg = trimmed.replace("\\", "/").rsplit("/", 1)[-1]
    return seg or trimmed


def repo_slug(url: str) -> str:
    """Best-effort ``owner/repo`` slug from a repo URL."""
    trimmed = _trim_repo_url(url)
    parts = [p for p in trimmed.replace("\\", "/").split("/") if p]
    if len(parts) >= 2:
        return f"{parts[-2]}/{parts[-1]}"
    return repo_name(url)


_ALLOWED_URL_SCHEMES = ("https://", "http://", "ssh://", "git://", "file://")

#: `C:\...` or `C:/...` — a Windows drive-letter path used as a local remote.
_WINDOWS_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


def validate_remote_url(url: str) -> str:
    """Refuse remotes git would read as an option or as a command to run.

    Two distinct hazards, both reachable from the connect-a-vault form:

    * A value starting with ``-`` lands in option position in the argv below, so
      ``--upload-pack=<cmd>`` would make git execute ``<cmd>``.
    * ``ext::``/``fd::`` are transport *helpers* — the remote names a program git
      runs, which is arbitrary code execution rather than a fetch.
    """
    if not url or url.startswith("-"):
        raise AttachError("that remote URL is not valid", "bad_remote_url")
    if "::" in url.split("/", 1)[0]:
        raise AttachError("remote URL uses a git transport helper", "bad_remote_url")
    if url.lower().startswith(_ALLOWED_URL_SCHEMES) or url.startswith(("/", ".", "~")):
        # An http(s) URL carrying userinfo (`https://user:token@host/...`) would
        # persist that credential into the clone argv and .git/config. Auth
        # belongs in the PAT store / a credential helper, never baked into the
        # remote — reject it. (ssh:// userinfo is an ssh login name, not a
        # bearer token, so it is left alone.)
        low = url.lower()
        if low.startswith(("http://", "https://")):
            authority = url.split("//", 1)[1].split("/", 1)[0]
            if "@" in authority:
                raise AttachError(
                    "remote URL must not contain embedded credentials", "bad_remote_url"
                )
        return url
    # Windows drive path (`C:\vaults\notes` or `C:/vaults/notes`): a single
    # drive letter before the colon with a path separator right after. An
    # scp-like host is never one letter followed by a separator, so this
    # cannot be confused with the `git@host:path` form below, and it can be
    # neither an option (no leading `-`) nor a transport helper (no `::`).
    if _WINDOWS_DRIVE_PATH.match(url):
        return url
    # scp-like `git@host:owner/repo` — one colon, never a `::` helper.
    host, sep, path = url.partition(":")
    if sep and path and "@" in host:
        return url
    raise AttachError("that remote URL is not valid", "bad_remote_url")


def validate_ref(ref: str) -> str:
    """A branch name must not be readable as an option either."""
    if not ref or ref.startswith("-") or " " in ref:
        raise AttachError("that branch name is not valid", "bad_branch")
    return ref


def is_local_remote(url: str) -> bool:
    """True for a ``file://`` URL or a bare filesystem path (incl. ``C:\\``)."""
    return (
        url.startswith("file://")
        or url.startswith("/")
        or url.startswith(".")
        or bool(_WINDOWS_DRIVE_PATH.match(url))
    )


#: The only origin the stored PAT is scoped to. `git config` matches
#: `http.<url>.*` by URL prefix, so the trailing slash keeps it to github.com.
GITHUB_ORIGIN = "https://github.com/"


#: Repository config keys that can name a program for git to run. A vault is an
#: ordinary checkout with an agent-writable ``.git/config``, so each of these is
#: attacker-controllable and would execute outside any sandbox as a side effect
#: of a note being synced. Applied as environment rather than per-call ``-c`` so
#: every invocation is covered at one chokepoint. Mirrors dev_fleet's
#: ``_GIT_ENV_NEUTRALIZERS``.
_GIT_NEUTRALIZERS: list[tuple[str, str]] = [
    ("core.fsmonitor", "false"),
    ("core.hooksPath", os.devnull),
    ("credential.helper", ""),
    ("core.sshCommand", "ssh"),
    # A repo that sets commit.gpgSign=true plus a malicious gpg.program would
    # run that program when the app commits during sync. Signing is never wanted
    # for the app's own auto-commits, so force it off (and tag signing, for the
    # same reason) rather than trust the vault's config. Signature VERIFICATION
    # is the other trigger: `git merge` on a fetched commit that carries a
    # gpgsig header would invoke gpg.program when merge.verifySignatures=true, so
    # that (and the pull equivalent) is forced off too — the app never verifies
    # signatures, so the only reason git would exec gpg.program is an attacker's.
    ("commit.gpgSign", "false"),
    ("tag.gpgSign", "false"),
    ("merge.verifySignatures", "false"),
    ("pull.verifySignatures", "false"),
    # Local/file transports exec the config-named pack programs directly —
    # GIT_ALLOW_PROTOCOL does not gate them because they are not a protocol.
    # Every invocation here uses the literal remote name `origin`, so pinning
    # these two keys restores git's own defaults over anything in .git/config.
    ("remote.origin.uploadpack", "git-upload-pack"),
    ("remote.origin.receivepack", "git-receive-pack"),
]


def _auth_env(pat: Optional[str]) -> dict[str, str]:
    """Environment neutralizing repo config, plus a PAT as a one-shot header.

    ``GIT_CONFIG_COUNT``/``KEY``/``VALUE`` carry the same precedence as ``git
    -c`` and so override every config file, including the repository's own.
    Unlike ``git -c`` they are not copied into a newly cloned repository's
    config, so a clone never keeps the token on disk.

    The neutralizers and the token share ONE numbered sequence — two separate
    ``GIT_CONFIG_COUNT`` blocks cannot coexist, and the later would silently
    drop the earlier.
    """
    entries = list(_GIT_NEUTRALIZERS)
    if pat:
        # GitHub accepts the token as the basic-auth username with any password.
        basic = base64.b64encode(f"{pat}:x-oauth-basic".encode()).decode()
        # Scope the header to GitHub. A bare `http.extraHeader` applies to EVERY
        # https remote, so syncing a vault hosted anywhere else would hand that
        # host the user's GitHub token. The stored PAT comes from GitHub (the
        # settings field or `gh auth token`), so github.com is the only origin it
        # belongs to — a self-hosted Enterprise remote needs its own credential
        # and is not covered here.
        entries.append((f"http.{GITHUB_ORIGIN}.extraHeader", f"Authorization: Basic {basic}"))

    env = {
        # Refuse `ext::`/custom remote helpers at the protocol layer as well as
        # in `validate_remote_url`. `file` stays allowed because attaching a
        # local vault is a supported flow.
        "GIT_ALLOW_PROTOCOL": "https:ssh:file",
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_CONFIG_COUNT": str(len(entries)),
    }
    for i, (key, value) in enumerate(entries):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env


def _windows_git_bin_dirs() -> tuple[str, ...]:
    """Trusted, fixed Git-for-Windows install locations (never PATH).

    Covers both the machine-wide install under ``%ProgramFiles%`` and the
    per-user install under ``%LOCALAPPDATA%\\Programs\\Git`` — the latter is the
    no-admin install `windows-install.md` recommends and what `kirocrew doctor`
    itself detects, so omitting it made every vault op fail closed with
    ``git_failed`` on an ordinary Windows machine. These are fixed install roots,
    not workspace-writable, so trusting them does not reopen the PATH-hijack hole.
    """
    dirs: list[str] = []
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    localappdata = os.environ.get("LOCALAPPDATA", "")
    roots = (
        [program_files, os.path.join(localappdata, "Programs")]
        if localappdata
        else [program_files]
    )
    for root in roots:
        dirs.append(os.path.join(root, "Git", "cmd"))
        dirs.append(os.path.join(root, "Git", "bin"))
        dirs.append(os.path.join(root, "Git", "mingw64", "bin"))
    return tuple(dirs)


#: Trusted absolute directories to resolve the ``git`` binary from, tried in
#: order BEFORE ``PATH``: a workspace-writable entry earlier in PATH could
#: otherwise shadow ``git`` with a planted binary that then runs unsandboxed on
#: the next sync. On a normal POSIX host git lives in one of these, so PATH is
#: never consulted there. The Windows entries cover both the machine-wide and
#: the per-user Git-for-Windows install roots (the backend itself is macOS/Linux
#: only; these serve Windows dev hosts and CI test runners).
_GIT_BIN_DIRS: tuple[str, ...] = (
    "/usr/bin",
    "/bin",
    "/usr/local/bin",
    "/opt/homebrew/bin",
) + _windows_git_bin_dirs()
_git_bin_memo: Optional[str] = None


def _git_bin() -> str:
    """Absolute path to a trusted ``git``, resolved once. Fails closed.

    Only the trusted system directories above are searched — never ``PATH`` —
    so a planted binary in a workspace-writable PATH entry cannot win. The
    Windows entries cover both the machine-wide and the per-user
    Git-for-Windows install roots (Windows dev hosts and CI test runners).
    Fails closed if git is not found in a trusted location.
    """
    global _git_bin_memo
    if _git_bin_memo is not None:
        return _git_bin_memo
    override = os.environ.get("MD_NOTEBOOK_GIT_BIN")
    if override:
        if os.path.isfile(override) and os.access(override, os.X_OK):
            _git_bin_memo = override
            return override
        raise GitError(f"MD_NOTEBOOK_GIT_BIN is not an executable file: {override}")
    for directory in _GIT_BIN_DIRS:
        for name in ("git", "git.exe"):
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                _git_bin_memo = candidate
                return candidate
    raise GitError(
        "no trusted git binary found in a system location "
        f"({', '.join(_GIT_BIN_DIRS)}); install git or set MD_NOTEBOOK_GIT_BIN"
    )


#: PATH handed to POSIX subprocesses that dispatch to child helpers, so those
#: helpers resolve from trusted system directories rather than an agent-writable
#: entry inherited from the gateway's PATH. Two callers need it: git (ssh for
#: ssh:// remotes, credential helpers, git-remote-*) and the file-manager reveal
#: in `server.py` (`xdg-open` is a shell script that searches PATH for `gio`,
#: `gvfs-open`, `exo-open`, ...). POSIX only — on Windows (test runners) git's
#: helpers live beside the install, so the inherited PATH is kept there.
TRUSTED_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


async def run_git(
    args: list[str],
    cwd: Optional[str] = None,
    *,
    pat: Optional[str] = None,
    check: bool = True,
    timeout: int = GIT_TIMEOUT_SEC,
) -> tuple[int, str, str]:
    """Run a git command. Returns (returncode, stdout, stderr).

    Never runs through a shell, so no argument can be interpreted as one.
    """
    env = {
        **os.environ,
        # Keep git non-interactive: a credential prompt in a background process
        # would hang the request until the timeout.
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "GCM_INTERACTIVE": "never",
        **_auth_env(pat),
    }
    if os.name == "posix":
        # Pin PATH so git's own child helpers (ssh, credential helpers,
        # git-remote-*) resolve from trusted system dirs, not an agent-writable
        # entry inherited from the gateway's PATH.
        env["PATH"] = TRUSTED_PATH
    proc = await asyncio.create_subprocess_exec(
        _git_bin(),
        *args,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise GitError(f"git {args[0]} timed out after {timeout}s") from None
    stdout = out.decode("utf-8", "replace")
    stderr = err.decode("utf-8", "replace")
    if check and proc.returncode != 0:
        tail = " ".join(stderr.strip().splitlines()[-3:])
        raise GitError(f"git {args[0]} failed ({proc.returncode}): {tail}")
    return proc.returncode or 0, stdout, stderr


@dataclass
class FileChange:
    """One working-tree change relative to HEAD."""

    path: str
    kind: str  # added | modified | deleted

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "kind": self.kind}


async def _current_branch(dir_: str) -> Optional[str]:
    """Current branch name, or None on a detached HEAD or empty repo."""
    code, out, _ = await run_git(["rev-parse", "--abbrev-ref", "HEAD"], dir_, check=False)
    name = out.strip()
    if code != 0 or not name or name == "HEAD":
        return None
    return name


async def _remote_url(dir_: str) -> Optional[str]:
    """Configured ``remote.origin.url``, or None when unset."""
    code, out, _ = await run_git(["config", "--get", "remote.origin.url"], dir_, check=False)
    return out.strip() or None if code == 0 else None


async def _git_dir(dir_: str) -> Optional[str]:
    """The canonical (realpath) git directory backing this checkout.

    For an attached worktree ``.git`` is a FILE (``gitdir: <path>``) that points
    at the real git dir; an agent could rewrite it to redirect sync into an
    unrelated same-origin checkout. Persisting this at attach/clone time and
    re-checking before sync detects that redirection."""
    code, out, _ = await run_git(["rev-parse", "--absolute-git-dir"], dir_, check=False)
    if code != 0 or not out.strip():
        return None
    try:
        return await asyncio.to_thread(lambda: os.path.realpath(out.strip()))
    except OSError:
        return None


async def _all_origin_urls(dir_: str) -> list[str]:
    """Every configured origin URL — the fetch urls (``remote.origin.url``) AND
    the push urls (``remote.origin.pushurl``). Both keys are multi-valued and
    git pushes to ALL of them, so the trusted-remote check must see every value.
    A ``--get`` (first value only) would let an agent hide an attacker URL behind
    a trusted one."""
    urls: list[str] = []
    for key in ("remote.origin.url", "remote.origin.pushurl"):
        code, out, _ = await run_git(["config", "--get-all", key], dir_, check=False)
        if code == 0:
            urls.extend(u.strip() for u in out.splitlines() if u.strip())
    return urls


def _norm_remote(url: Optional[str]) -> str:
    """Normalize a remote URL for comparison (trailing slash / ``.git`` suffix
    are cosmetic and must not defeat the trusted-URL check)."""
    if not url:
        return ""
    u = url.strip().rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    return u


async def _has_head(dir_: str) -> bool:
    """True when the repo has at least one commit."""
    code, _, _ = await run_git(["rev-parse", "--verify", "HEAD"], dir_, check=False)
    return code == 0


async def clone_vault(
    *,
    url: str,
    dir_: str,
    vault_id: str,
    branch: str = "main",
    subfolder: Optional[str] = None,
    name: Optional[str] = None,
    pat: Optional[str] = None,
    read_only: bool = False,
) -> dict[str, Any]:
    """Clone a remote vault and return its descriptor.

    Deliberately a FULL clone. The TypeScript original defaulted to ``depth: 1``,
    but most servers refuse a push from a shallow clone, which would break the
    app's own sync. Note vaults are text, so full history is cheap.
    """
    # `--` terminates option parsing, so even a future caller passing something
    # option-shaped cannot turn the positional remote into a git flag.
    args = [
        "clone",
        "--branch",
        validate_ref(branch),
        "--single-branch",
        "--",
        validate_remote_url(url),
        dir_,
    ]
    await run_git(args, pat=pat, timeout=GIT_NETWORK_TIMEOUT_SEC)
    vault: dict[str, Any] = {
        "id": vault_id,
        "name": name or repo_name(url),
        "repo": repo_slug(url),
        "localPath": dir_,
        "branch": branch,
        "readOnly": read_only,
        # The trusted remote URL, persisted so sync can detect an agent that
        # later repoints the vault's .git/config at a different remote.
        "remoteUrl": validate_remote_url(url),
        # The canonical git dir, persisted so sync can detect a redirected
        # `.git` pointer (worktree/submodule checkouts store `.git` as a file).
        "gitDir": await _git_dir(dir_),
    }
    if subfolder:
        vault["subfolder"] = subfolder
    return vault


async def attach_vault(
    *,
    dir_: str,
    vault_id: str,
    subfolder: Optional[str] = None,
    name: Optional[str] = None,
    read_only: bool = False,
) -> dict[str, Any]:
    """Adopt an existing local git working tree as a vault — no second copy.

    Reads the repo's own remote and current branch so sync targets exactly what
    the user's git already tracks. A repo with NO remote is accepted and marked
    ``localOnly``: sync then commits to local history and never fetches or
    pushes.
    """
    path = Path(dir_)

    def _probe() -> tuple[bool, bool, bool, bool]:
        """Stat the directory once, off the event loop.

        .git is a directory in a normal clone and a file in a worktree or
        submodule — both are real working trees, so existence is the test.
        """
        return (
            path.exists(),
            path.is_dir(),
            (path / ".git").exists(),
            (path / subfolder).is_dir() if subfolder else True,
        )

    exists, is_dir, has_git, has_subfolder = await asyncio.to_thread(_probe)
    if not exists:
        raise AttachError(f"Folder not found: {dir_}", "ENOENT")
    if not is_dir:
        raise AttachError(f"Not a folder: {dir_}", "ENOTDIR")
    if not has_git:
        raise AttachError(f"Not a git repository (no .git found): {dir_}", "ENOGIT")
    if not has_subfolder:
        raise AttachError(f"Subfolder not found in repo: {subfolder}", "ENOSUB")

    # A repo with no remote is a valid vault: notes live in local git history
    # and sync degrades to a local commit. Requiring a remote made the common
    # `git init` scratch folder unusable for no benefit.
    url = await _remote_url(dir_)

    branch = await _current_branch(dir_) or "main"
    vault: dict[str, Any] = {
        "id": vault_id,
        "name": name or (repo_name(url) if url else "") or path.name,
        "repo": repo_slug(url) if url else "",
        "localPath": dir_,
        "branch": branch,
        "readOnly": read_only,
        # Recorded explicitly rather than inferred from a null `remoteUrl`,
        # because `.git/config` is agent-writable: an origin appearing later is
        # not a user decision this app can assume, so sync refuses instead of
        # silently starting to push the user's notes to it.
        "localOnly": url is None,
        # The remote as it stood at attach time — sync refuses if it is later
        # repointed in the vault's agent-writable .git/config.
        "remoteUrl": url,
        # Canonical git dir at attach time; sync refuses if `.git` is redirected.
        "gitDir": await _git_dir(dir_),
    }
    if subfolder:
        vault["subfolder"] = subfolder
    return vault


async def status(dir_: str, subfolder: Optional[str] = None) -> list[FileChange]:
    """Working-tree changes relative to HEAD.

    Compares the working tree directly against HEAD (the index is not a
    separate state here), matching the status-matrix semantics of the original:
    absent at HEAD but present now is ``added``, gone is ``deleted``, different
    is ``modified``. Untracked files count as added.
    """
    # A repo-defined `clean` filter fires during the diff below (the working
    # tree is filtered to compare against HEAD), so the driver refusal must
    # gate every status read — the notes listing reaches here via
    # refresh_statuses(), not only sync(). Fails closed, same as sync().
    driver = await repo_supplied_driver(dir_)
    if driver:
        raise GitError(
            "this vault's repository defines its own git driver or working-tree "
            f"redirect ({driver}), which reading its status would act on"
        )

    prefix = f"{subfolder.rstrip('/')}/" if subfolder else None
    changes: list[FileChange] = []

    if await _has_head(dir_):
        # --no-ext-diff: a vault's `diff.external=<payload>` in .git/config
        # would otherwise run that program when this diff executes.
        _, out, _ = await run_git(["diff", "--no-ext-diff", "--name-status", "-z", "HEAD"], dir_)
        # -z output is NUL-separated: STATUS \0 PATH \0 STATUS \0 PATH ...
        # Rename/copy records carry two paths, so consume by record type.
        fields = [f for f in out.split("\0") if f != ""]
        i = 0
        kinds = {"A": "added", "M": "modified", "D": "deleted", "T": "modified"}
        while i < len(fields):
            code = fields[i]
            letter = code[0]
            if letter in ("R", "C") and i + 2 < len(fields):
                # A rename reads as the old path deleted and the new one added,
                # which is how the status-matrix original reported it too.
                changes.append(FileChange(path=fields[i + 1], kind="deleted"))
                changes.append(FileChange(path=fields[i + 2], kind="added"))
                i += 3
                continue
            if letter in kinds and i + 1 < len(fields):
                changes.append(FileChange(path=fields[i + 1], kind=kinds[letter]))
                i += 2
                continue
            i += 1

    # Untracked files are additions the diff above cannot see.
    _, untracked, _ = await run_git(
        ["ls-files", "--others", "--exclude-standard", "-z"], dir_
    )
    for rel in untracked.split("\0"):
        if rel:
            changes.append(FileChange(path=rel, kind="added"))

    if prefix:
        changes = [c for c in changes if c.path.startswith(prefix)]
    # The local trash lives inside the vault, so git sees it. Filtering here
    # keeps a trashed note out of the commit message and out of the notes
    # listing's pending badge; `auto_commit` additionally excludes it from the
    # pathspec, because `add -A` stages from the working tree and never consults
    # this list.
    return [c for c in changes if not in_trash(c.path)]


def _commit_message(changes: list[FileChange]) -> str:
    """``Update <filename>`` for a single file, else a count."""
    if len(changes) == 1:
        return f"Update {changes[0].path.split('/')[-1]}"
    return f"Update {len(changes)} files"


async def _drop_staged(dir_: str, changes: list[FileChange]) -> list[FileChange]:
    """Remove paths that are STAGED relative to HEAD.

    A vault is an ordinary git repository the user can also drive from a terminal,
    and a staged path is a commit they are composing by hand. The unattended
    autosave must not touch it, in either of two ways:

    * PARTIALLY staged (``git add -p``) — ``git add`` would overwrite the index
      entry with the full working copy and commit it, so content the user
      deliberately held back is committed by a timer they never triggered.
    * FULLY staged — ``git commit -- <path>`` commits that path ALONE, lifting it
      out of the multi-file commit the user was assembling. No content is lost, but
      the composition is, and they cannot tell it happened until they look at the
      log.

    So the test is simply "is it staged", not "does the index differ from the
    working tree": intent, not divergence. Only the UNATTENDED path filters — a
    deliberate Sync still stages everything, because the user chose the moment.

    Excluding just those paths (rather than skipping the whole tick) keeps autosave
    working for every other note, and a held-back path resumes as soon as the
    user's own commit lands.
    """
    if not changes:
        return changes
    pathspec = ["--", *(f":(literal){c.path}" for c in changes)]
    # `--cached` is index-vs-HEAD: exactly "the user has staged this".
    _, staged_out, _ = await run_git(
        ["diff", "--no-ext-diff", "--cached", "--name-only", "-z", *pathspec], dir_
    )
    staged = {p for p in staged_out.split("\0") if p}
    if not staged:
        return changes
    return [c for c in changes if c.path not in staged]


async def auto_commit(
    dir_: str,
    message: Optional[str] = None,
    *,
    subfolder: Optional[str] = None,
    notes_only: bool = False,
    author_name: str = DEFAULT_AUTHOR_NAME,
    author_email: str = DEFAULT_AUTHOR_EMAIL,
) -> dict[str, Any]:
    """Stage the vault's working-tree changes and commit. No-op on a clean tree.

    Staging is confined to ``subfolder`` when the vault is scoped to one. An
    unscoped ``add -A`` would sweep in everything else in the repository — a
    vault scoped to ``notes/`` inside a repo the user also works in would commit
    and push that unrelated work under a note-shaped commit message.

    ``notes_only`` narrows staging further, to exactly the changed ``.md`` files
    the user has not staged for a commit of their own (see ``_drop_staged``), and
    is what the periodic autosave uses. Without it an UNATTENDED commit would
    capture any file that happens to sit in the vault — a temporary secret a user
    dropped there and meant to delete before syncing would be recorded in local
    history by a timer they never triggered, and the next push would put that blob
    on the remote permanently. A deliberate Sync stages every change `status()`
    reported; the difference is that the user chose the moment.
    """
    changes = await status(dir_, subfolder)
    if notes_only:
        changes = [c for c in changes if c.path.lower().endswith(".md")]
        changes = await _drop_staged(dir_, changes)
    if not notes_only and len(changes) > MAX_STAGED_PATHS:
        # A user-initiated Sync must not silently commit a SUBSET. `status()`
        # reports a rename as two independent entries — the old path deleted, the
        # new one added (see `status`) — and the slice below sorts by path, so the
        # cutoff can fall between them. Committing and PUSHING only the deletion
        # half makes the note look deleted to every other clone, while the UI
        # reports the sync as a success. The autosave keeps the cap (it commits
        # locally and pushes nothing, so a split rename is repaired by the next
        # tick), but here the honest outcome is to refuse and say why.
        raise GitError(
            f"this vault has {len(changes)} changed files, more than the "
            f"{MAX_STAGED_PATHS} one sync can stage at once. Autosave is working "
            "through them in batches — sync again once the count is lower, or "
            "commit them yourself with git"
        )
    # Bounded so a huge first sweep cannot build an argv past the OS limit. Only
    # the autosave reaches this: the remainder is picked up by the next tick, a
    # delay rather than a loss, and nothing has been pushed.
    changes = sorted(changes, key=lambda c: c.path)[:MAX_STAGED_PATHS]
    if not changes:
        return {"oid": None, "message": "", "committed": []}

    # Stage EXACTLY the paths `status()` reported, named individually — never a
    # directory-wide `add -A`. `status()` filters the trash out (see `in_trash`),
    # so this is what keeps the trash — including a PRE-EXISTING Obsidian one in a
    # freshly attached vault — out of every commit. It is the ONLY mechanism doing
    # that: nothing writes a git ignore rule for `.trash/`, so a directory-wide add
    # would sweep it up and push it.
    #
    # `:(literal)` disables Git pathspec magic on every one: a note filename is
    # user-controlled, and a name like `:(top,glob)**` read as a magic pathspec
    # would widen the add to unrelated work elsewhere in the repository.
    pathspec = ["--", *(f":(literal){c.path}" for c in changes)]
    await run_git(["add", "-A", *pathspec], dir_)
    final_message = message or _commit_message(changes)
    ident = [
        "-c",
        f"user.name={author_name}",
        "-c",
        f"user.email={author_email}",
    ]
    # The pathspec on the commit as well: `git commit -- <path>` commits only
    # the named paths, so a file the user had staged elsewhere in the repo
    # stays staged instead of riding along under a note-shaped message.
    await run_git([*ident, "commit", "--no-gpg-sign", "-m", final_message, *pathspec], dir_)
    _, oid, _ = await run_git(["rev-parse", "HEAD"], dir_)
    return {
        "oid": oid.strip(),
        "message": final_message,
        "committed": [c.to_dict() for c in changes],
    }


async def _file_at_commit(dir_: str, oid: str, path: str) -> str:
    """A file's content at a commit, or empty string when absent there."""
    code, out, _ = await run_git(["show", f"{oid}:{path}"], dir_, check=False)
    return out if code == 0 else ""


async def repo_supplied_driver(dir_: str) -> str:
    """Name of a repo-defined merge/filter driver git would execute, else "".

    The `-c` neutralizers above close the fixed keys (`core.hooksPath`,
    `core.fsmonitor`, `core.sshCommand`, `credential.helper`). Drivers are a
    different shape: `.gitattributes` names one (`merge=foo`) and config defines
    what it runs (`merge.foo.driver`, `filter.foo.smudge`/`.process`), so the key
    space is unbounded and cannot be enumerated with `-c`. `git merge` and any
    checkout would execute it with the gateway's privileges.

    So this refuses instead of neutralizing, following
    `dashboard/handlers/worktree.py::_checkout_filter`. Driver definitions can
    only come from a config FILE — never from `.gitattributes`, and never from a
    remote, since clone does not transfer config — so the repository-supplied
    scopes are the two git reads from inside the repo: `--local` (`.git/config`)
    and `--worktree` (`config.worktree`, live only under
    `extensions.worktreeConfig`). `--includes` is mandatory: for a specific-scope
    query git defaults include-following OFF, so a driver reached via
    `include.path` would resolve at merge time yet be invisible here.

    Global and system config are deliberately not probed — that is the user's own
    machine configuration, not something the repository supplies.

    Fails CLOSED: a probe that errors means we cannot prove the repo is
    driver-free, so the caller must not proceed.
    """
    scopes = ["--local"]
    code, out, _ = await run_git(
        ["config", "--local", "--includes", "--get", "extensions.worktreeConfig"],
        dir_,
        check=False,
    )
    if code == 0 and out.strip().lower() == "true":
        scopes.append("--worktree")

    for scope in scopes:
        code, out, err = await run_git(
            ["config", scope, "--includes", "--name-only", "--list"], dir_, check=False
        )
        if code != 0:
            # An empty local config exits 1 with no output; anything else means
            # the probe itself failed and we cannot clear the repo.
            if out.strip() == "" and err.strip() == "":
                continue
            return "unprobeable config"
        for key in out.splitlines():
            k = key.strip().lower()
            if (k.startswith("merge.") and k.endswith((".driver",))) or (
                k.startswith("filter.") and k.endswith((".smudge", ".clean", ".process"))
            ):
                return key.strip()
            # `core.gitProxy` names a program git runs for the git:// transport
            # — no benign use in this app, so refuse outright.
            if k == "core.gitproxy":
                return key.strip()
            # Repository-local HTTP proxy / TLS-override keys are a credential
            # exfiltration vector: a vault that sets `http.proxy=<attacker>`
            # (optionally with `http.sslVerify=false` / a swapped CA) would make
            # the PAT-bearing sync request flow through, or be trusted by, an
            # attacker endpoint. A vault has no legitimate reason to set these —
            # a user behind a real proxy configures it in their GLOBAL config,
            # which this repo-scope probe never sees — so refuse them.
            if k.startswith("http.") and (
                k.endswith(".proxy")
                or "sslverify" in k
                or "sslcainfo" in k
                or "sslcapath" in k
                or "sslcert" in k
                or "sslkey" in k
                or "proxyauthmethod" in k
            ):
                return key.strip()
            # `url.<base>.insteadOf` / `url.<base>.pushInsteadOf` rewrite the
            # effective remote URL at git's transport layer. A repo-local rewrite
            # could redirect the PAT-bearing push to an attacker endpoint while
            # `remote.origin.url` still reads as the trusted URL — so the
            # trusted-remote check in sync() would not catch it. A vault has no
            # legitimate reason to set these, so refuse.
            if k.startswith("url.") and (
                k.endswith(".insteadof") or k.endswith(".pushinsteadof")
            ):
                return key.strip()
            # `core.worktree` redirects git's working tree. A blanket refusal
            # would break a legitimately-supported vault shape: git itself sets
            # core.worktree for submodule / `--separate-git-dir` checkouts (see
            # attach_vault's docstring), pointing it back AT the checkout. So
            # refuse only when it resolves OUTSIDE the vault — the case where
            # `add -A` would stage an attacker-chosen tree. Ask git for the
            # effective worktree rather than parsing the (relative-to-GIT_DIR)
            # value ourselves.
            if k == "core.worktree":
                code2, top, _ = await run_git(
                    ["rev-parse", "--show-toplevel"], dir_, check=False
                )
                if code2 != 0:
                    return "core.worktree (unverifiable)"  # fail closed
                try:
                    # realpath is sync filesystem I/O — offload it so a stalled
                    # network vault cannot freeze the gateway event loop.
                    top_s = top.strip()
                    redirected = await asyncio.to_thread(
                        lambda: os.path.realpath(top_s) != os.path.realpath(dir_)
                    )
                except OSError:
                    return "core.worktree (unverifiable)"
                if redirected:
                    return "core.worktree (redirected outside the vault)"
    return ""


async def sync(
    dir_: str,
    *,
    branch: Optional[str] = None,
    pat: Optional[str] = None,
    subfolder: Optional[str] = None,
    trusted_remote: Optional[str] = None,
    trusted_gitdir: Optional[str] = None,
    local_only: bool = False,
    commit_only: bool = False,
    author_name: str = DEFAULT_AUTHOR_NAME,
    author_email: str = DEFAULT_AUTHOR_EMAIL,
) -> dict[str, Any]:
    """Commit local changes, fetch, merge and push.

    On a merge conflict NOTHING is overwritten: the merge is aborted so the
    working tree keeps the local content, and the result carries every
    conflicted path with both the local and remote versions for the UI to
    present.

    ``local_only`` is a vault attached from a repo with no remote: the run stops
    after the commit — there is nothing to fetch, merge or push.

    ``commit_only`` stops after the commit for ANY vault. It backs the periodic
    autosave, so it deliberately skips the remote-identity checks: nothing it
    does can reach a remote, and a vault whose remote drifted must still get its
    edits into local history rather than silently stop saving.
    """
    # Validate before ANY git call: `target` is passed as a positional to
    # `fetch`/`merge`/`push`, so a persisted branch like `--upload-pack=<prog>`
    # would land in option position and make git execute it. validate_ref
    # Prefer the branch actually checked out over the vault's stored `branch`:
    # if the working tree was switched externally (`git checkout feature`),
    # committing HEAD and pushing the stale stored `main` would land the feature
    # commits on the wrong remote branch. Sync the branch the user is on.
    target = validate_ref(await _current_branch(dir_) or branch or "main")
    # Remote identity is only load-bearing for a run that can PUSH. A
    # `commit_only` run cannot, so it skips these checks entirely — otherwise a
    # user who repointed their own remote in git would find autosave has stopped
    # writing history, which is the opposite of a safety property.
    if not commit_only:
        origin_urls = await _all_origin_urls(dir_)
        if local_only:
            # This vault was attached from a repo with NO remote, so a configured
            # origin now means `.git/config` changed since. That file is
            # agent-writable, so treat it as untrusted rather than as the user
            # opting in to a remote — pushing note history to it is exactly the
            # exfiltration the trusted-remote check below exists to prevent.
            if origin_urls:
                raise GitError(
                    "this vault was attached with no git remote, but one is configured "
                    "now — refusing to push to a remote it was not connected with"
                )
        elif not origin_urls:
            raise GitError(f"No remote.origin.url configured for {dir_}")

        # Refuse to sync if the vault's git remote no longer matches the URL it
        # was created/attached with. A vault's `.git/config` is agent-writable,
        # so a prompt-injected agent could repoint `remote.origin.url` (or
        # `pushurl`) at an attacker and have auto-sync upload the note history
        # there. The trusted URL is persisted in vaults.json, which is behind the
        # sensitive-path floor.
        if trusted_remote is not None:
            trusted = _norm_remote(trusted_remote)
            effective = await _all_origin_urls(dir_)
            if not effective or any(_norm_remote(u) != trusted for u in effective):
                raise GitError(
                    "this vault's git remote URL no longer matches the trusted URL it "
                    "was created with — refusing to sync to avoid pushing to an "
                    "unexpected remote"
                )

    # A worktree/submodule checkout stores `.git` as a file pointing at the real
    # git dir. An agent could rewrite that pointer to redirect commit/push into
    # an unrelated same-origin checkout. Verify the canonical git dir still
    # matches the one recorded at attach/clone time.
    if trusted_gitdir is not None:
        current_gitdir = await _git_dir(dir_)
        trusted_real = await asyncio.to_thread(os.path.realpath, trusted_gitdir)
        if current_gitdir is None or current_gitdir != trusted_real:
            raise GitError(
                "this vault's .git location no longer matches where it was "
                "created — refusing to sync to avoid acting on an unrelated "
                "repository"
            )

    # 0. Refuse a repository that defines its own driver, BEFORE any git command
    # that could invoke one. A `clean` filter fires during `status` and `add`,
    # not just at merge time, so this has to gate the whole operation rather than
    # sit in front of step 3.
    driver = await repo_supplied_driver(dir_)
    if driver:
        raise GitError(
            "this vault's repository defines its own git driver or working-tree "
            f"redirect ({driver}), which a sync would act on — resolve it yourself instead"
        )

    # 1. Commit local work so it is part of history before merging.
    pending = await status(dir_, subfolder)
    committed: list[dict[str, str]] = []
    if pending:
        result = await auto_commit(
            dir_,
            subfolder=subfolder,
            # An UNATTENDED commit stages only notes. A user-initiated Sync keeps
            # staging the whole scope, because the user chose that moment; a timer
            # did not, and a stray file in the vault must not enter history — and
            # then the remote — without them deciding to send it.
            notes_only=commit_only,
            author_name=author_name,
            author_email=author_email,
        )
        committed = result["committed"]

    # A local-only vault has nowhere to fetch from or push to, and a commit-only
    # run is asked to stop here. The commit above IS the whole operation, so
    # return before any network step rather than letting `fetch origin` fail on a
    # remote that does not exist.
    if local_only or commit_only:
        return {
            "pushed": False,
            "pulled": False,
            "committed": committed,
            "conflicts": [],
            "localOnly": local_only,
            "commitOnly": commit_only,
        }

    # 2. Fetch the remote tip.
    await run_git(
        ["fetch", "origin", target],
        dir_,
        pat=pat,
        timeout=GIT_NETWORK_TIMEOUT_SEC,
    )
    _, remote_oid_out, _ = await run_git(["rev-parse", "FETCH_HEAD"], dir_)
    remote_oid = remote_oid_out.strip()
    _, local_oid_out, _ = await run_git(["rev-parse", "HEAD"], dir_)
    local_oid = local_oid_out.strip()

    if remote_oid == local_oid:
        return {"pushed": False, "pulled": False, "committed": committed, "conflicts": []}

    # 3. Merge. A conflict is a normal outcome, not an error.
    ident = ["-c", f"user.name={author_name}", "-c", f"user.email={author_email}"]
    # Explicit flags, not just the config neutralizers: a vault's
    # `branch.<name>.mergeOptions=-S` injects `-S` into this merge and a
    # command-line option overrides config, so signing (and its gpg.program)
    # would run anyway. `--no-gpg-sign` / `--no-verify-signatures` on the argv
    # win over both, closing the merge path to gpg.program execution.
    code, _, _ = await run_git(
        [*ident, "merge", "--no-edit", "--no-gpg-sign", "--no-verify-signatures", remote_oid],
        dir_,
        check=False,
    )
    if code != 0:
        _, conflicted, _ = await run_git(
            ["diff", "--no-ext-diff", "--name-only", "--diff-filter=U", "-z"], dir_, check=False
        )
        paths = [p for p in conflicted.split("\0") if p]
        if not paths:
            # The merge failed for a reason OTHER than content conflicts — e.g.
            # `merge.ff=only` refusing a non-fast-forward, or a repo config that
            # rejects the merge. There is nothing for the user to resolve, and
            # returning an empty-conflict result would let the caller record a
            # successful-sync timestamp for a sync that did not happen.
            await run_git(["merge", "--abort"], dir_, check=False)
            raise GitError(
                "the remote could not be merged (no content conflict to resolve) "
                "— resolve it in a git client and sync again"
            )
        conflicts = [
            {
                "path": p,
                "local": await _file_at_commit(dir_, local_oid, p),
                "remote": await _file_at_commit(dir_, remote_oid, p),
            }
            for p in paths
        ]
        # Restore the pre-merge working tree — the local content stays on disk.
        await run_git(["merge", "--abort"], dir_, check=False)
        return {
            "pushed": False,
            "pulled": False,
            "committed": committed,
            "conflicts": conflicts,
        }

    # 4. Push the merged branch back. `--no-signed` on the argv overrides a
    # vault's `push.gpgSign=true`, which would otherwise sign the push cert with
    # the repo's gpg.program. `HEAD:<target>` pushes the commits that were
    # actually made (on the current branch — the one status/commit/merge acted
    # on) to the remote target branch: if the vault's checkout was switched to
    # another branch externally, pushing the stale `target` ref would report
    # success while the remote never received the note.
    push_code, _, push_err = await run_git(
        ["push", "--no-signed", "origin", f"HEAD:{target}"],
        dir_,
        pat=pat,
        check=False,
        timeout=GIT_NETWORK_TIMEOUT_SEC,
    )
    if push_code != 0:
        # Surface it. The merge landed locally, but the remote does NOT have the
        # notes — reporting success here would tell the user their work is
        # backed up when it is only on this machine.
        logger.warning("md-notebook: push to origin/%s failed: %s", target, push_err.strip())
        raise GitError(f"pulled and merged, but the push to {target} was rejected: {push_err.strip()}")
    return {
        "pushed": True,
        "pulled": True,
        "committed": committed,
        "conflicts": [],
    }
