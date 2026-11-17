"""Fetch an app from a git URL for ``kirocrew app install <url>``.

The CLI calls this in the operator's own process, so ``git`` runs with their SSH
keys, agent and credential helpers: a repository installs exactly when the person
running the command could ``git clone`` it. The gateway's registry clone cannot
offer that, because it runs as the gateway, without that agent, and clones a host
it does not trust credential-free.

Fetching only produces a folder. The caller hands that folder to the ordinary
local-directory install, so every gate that path applies (manifest validation,
admission, the disabled first record, the trust prompt at enable) applies
unchanged, and nothing here registers or runs app code. Each attempt clones into
a new folder of its own, ``<data home>/app-git-checkouts/<repository>-<random>/``,
which is kept: the installed record names the app's folder inside it as its
source, so ``git -C <checkout> pull`` followed by ``kirocrew app update <name>``
updates the app.

Before git runs, the URL must be an ``https://``, ``ssh://`` or scp-style
``user@host:path`` remote with no password or token in it: the transport allowlist
the registry applies to an index it fetches. The clone itself runs with no
repository hooks, no template directory, no submodules and only the https and ssh
protocols, so cloning executes nothing the repository supplies.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, get_app
from kiro_crew.apps.manifest import app_name_error
from kiro_crew.apps.registry import (
    _GIT_EXEC_NEUTRALIZER_PAIRS,
    _git_target_is_unsupported,
    _git_url_host,
    _is_supported_registry_transport,
    _strip_git_target_userinfo,
)
from kiro_crew.config.loader import config_dir
from kiro_crew.owner_only_files import ensure_directory
from kiro_crew.platform_compat import rmtree_force
from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.user_json import loads_user_json

logger = logging.getLogger(__name__)

#: The data-home folder that holds one checkout per app installed from a git URL.
GIT_CHECKOUTS_DIRNAME = "app-git-checkouts"

#: Seconds one ``git clone`` may take before it is stopped.
CLONE_TIMEOUT_S = 600
#: Seconds the ``rev-parse`` that reads the cloned commit may take.
_REV_PARSE_TIMEOUT_S = 30
MAX_URL_LEN = 2048

#: A branch or tag name ``git clone --branch`` accepts, with no ``..`` and no
#: leading ``-`` (``fullmatch``, so a trailing newline does not pass).
_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-./]*")
#: One segment of ``--subdirectory``.
_SUBDIR_SEGMENT_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_\-.]*")
#: A scp-style remote, ``user@host:path``.
_SCP_SHAPE_RE = re.compile(r"[^/\\@\s]+@[^/\\:\s]+:.+")
#: A ``<transport>::<address>`` remote helper address.
_TRANSPORT_HELPER_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*::.*")

#: Environment variables that would point ``git clone`` at another repository,
#: object store or template. The operator's other variables, ``SSH_AUTH_SOCK``,
#: ``GIT_SSH_COMMAND`` and their config included, are kept: they are how their
#: access to the repository works.
_GIT_LOCATION_ENV_KEYS = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_GRAFT_FILE",
        "GIT_INDEX_FILE",
        "GIT_NAMESPACE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PREFIX",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
        "GIT_TEMPLATE_DIR",
        "GIT_WORK_TREE",
    }
)

#: ``-c`` pairs on every git spawn here: the registry's hook and fsmonitor
#: neutralizers, then a protocol allowlist that also binds any ``insteadOf``
#: rewrite in the operator's config, no submodule recursion, and no detached-HEAD
#: lecture when ``--ref`` names a tag.
_CLONE_CONFIG_PAIRS: tuple[tuple[str, str], ...] = (
    *_GIT_EXEC_NEUTRALIZER_PAIRS,
    ("protocol.allow", "never"),
    ("protocol.https.allow", "always"),
    ("protocol.ssh.allow", "always"),
    ("submodule.recurse", "false"),
    ("advice.detachedHead", "false"),
)


class GitInstallError(Exception):
    """A git URL install source that could not be fetched, with the reason."""


@dataclass(frozen=True)
class FetchedApp:
    """A cloned app, ready for the local-directory install."""

    name: str
    checkout: Path
    app_dir: Path
    commit: str
    #: The branch checked out, or ``""`` when ``--ref`` named a tag (a detached HEAD).
    branch: str


def looks_like_git_url(source: str) -> bool:
    """Whether *source* is written as a git remote rather than a path.

    Broad on purpose: a remote this module will refuse (``http://``, ``file://``,
    ``ext::``) is still recognised, so it gets that refusal instead of "source is
    not a directory".
    """
    text = (source or "").strip()
    if "://" in text:
        return True
    return bool(_SCP_SHAPE_RE.fullmatch(text) or _TRANSPORT_HELPER_RE.fullmatch(text))


def is_git_install_source(source: str) -> bool:
    """Whether ``kirocrew app install`` should clone *source*.

    An existing path wins, so a local folder whose name merely looks like a remote
    still installs as a folder.
    """
    if not looks_like_git_url(source):
        return False
    try:
        return not Path(source).expanduser().exists()
    except (OSError, ValueError):
        return True


def _names_a_repository(url: str) -> bool:
    """Whether *url* has a repository path after its host."""
    _scheme, sep, rest = url.partition("://")
    path = rest.partition("/")[2] if sep else url.partition("@")[2].partition(":")[2]
    return bool(path.strip("/"))


def validate_git_url(url: str) -> str:
    """Return *url* when it may be cloned, else raise :class:`GitInstallError`."""
    if not url or len(url) > MAX_URL_LEN:
        raise GitInstallError("the git URL is empty or too long")
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        raise GitInstallError("the git URL contains whitespace or a control character")
    if _git_target_is_unsupported(url):
        raise GitInstallError(
            "the git URL has a query (?), a fragment (#) or a colon in its user name, "
            "none of which is accepted"
        )
    lowered = url.lower()
    if lowered.startswith(("http://", "git://")):
        raise GitInstallError(
            "plaintext http:// and git:// remotes are refused, because anything on the "
            "network path could swap the code; use the https:// or ssh form of the URL"
        )
    if _strip_git_target_userinfo(url) != url or (
        lowered.startswith("https://") and "@" in url[len("https://") :].split("/", 1)[0]
    ):
        raise GitInstallError(
            "the git URL carries a password or token; remove it and let git "
            "authenticate with your SSH key or a credential helper"
        )
    if (
        not _is_supported_registry_transport(url)
        or not _git_url_host(url)
        or not _names_a_repository(url)
    ):
        raise GitInstallError(
            "unsupported git URL; use https://host/org/repo, ssh://[user@]host/org/repo "
            "or user@host:org/repo"
        )
    return url


def validate_ref(ref: str | None) -> str | None:
    """Return *ref* when ``git clone --branch`` may take it, else raise."""
    if ref is None:
        return None
    if not _REF_RE.fullmatch(ref) or ".." in ref or ref.endswith(("/", ".", ".lock")):
        raise GitInstallError(f"--ref {ref!r} is not a branch or tag name")
    return ref


def validate_subdirectory(subdirectory: str | None) -> tuple[str, ...]:
    """The segments of *subdirectory* (forward or back slashes), else raise."""
    if subdirectory is None:
        return ()
    parts = tuple(part for part in re.split(r"[\\/]", subdirectory) if part)
    if (
        not parts
        or subdirectory.startswith(("/", "\\"))
        or any(not _SUBDIR_SEGMENT_RE.fullmatch(part) for part in parts)
    ):
        raise GitInstallError(
            f"--subdirectory {subdirectory!r} must be a relative folder inside the "
            "repository, without '..'"
        )
    return parts


def git_checkouts_root() -> Path:
    """``<data home>/app-git-checkouts``: one checkout per git URL install attempt."""
    return config_dir() / GIT_CHECKOUTS_DIRNAME


def _git_on_path() -> bool:
    return shutil.which("git") is not None


def _git_config_argv() -> list[str]:
    argv: list[str] = []
    for key, value in _CLONE_CONFIG_PAIRS:
        argv += ["-c", f"{key}={value}"]
    return argv


def clone_argv(url: str, dest: Path, ref: str | None) -> list[str]:
    """The ``git clone`` command line for *url* into *dest*."""
    argv = [
        "git",
        *_git_config_argv(),
        "clone",
        "--depth",
        "1",
        "--single-branch",
        "--no-recurse-submodules",
        "--template=",
    ]
    if ref:
        argv += ["--branch", ref]
    return [*argv, "--", url, str(dest)]


def _clone_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in _GIT_LOCATION_ENV_KEYS}


def _run_clone(url: str, dest: Path, ref: str | None) -> None:
    """Clone *url* into *dest* with the operator's terminal attached.

    stdin, stdout and stderr are inherited, so git's progress, its own error and
    any prompt the operator's setup raises (an SSH host key, a key passphrase)
    reach them as they would from a plain ``git clone``.
    """
    try:
        proc = subprocess.run(
            clone_argv(url, dest, ref),
            env=_clone_env(),
            cwd=str(dest.parent),
            timeout=CLONE_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitInstallError("installing from a git URL needs git, which is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitInstallError(
            f"git clone did not finish within {CLONE_TIMEOUT_S // 60} minutes"
        ) from exc
    if proc.returncode != 0:
        raise GitInstallError(
            f"git clone failed (exit {proc.returncode}); git's message is above. The "
            "clone uses your own git setup, so check that `git clone` of this URL "
            "works in this shell"
        )


def _cloned_head(checkout: Path) -> tuple[str, str]:
    """``(commit, branch)`` the clone checked out; ``branch`` is ``""`` on a tag.

    Both are ``""`` when git cannot say.
    """
    try:
        proc = subprocess.run(
            [
                "git",
                *_git_config_argv(),
                "-C",
                str(checkout),
                "rev-parse",
                "HEAD",
                "--abbrev-ref",
                "HEAD",
            ],
            env=_clone_env(),
            capture_output=True,
            timeout=_REV_PARSE_TIMEOUT_S,
            check=False,
            **UTF8_TEXT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "", ""
    lines = proc.stdout.split() if proc.returncode == 0 else []
    if len(lines) != 2 or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", lines[0]):
        return "", ""
    commit, branch = lines
    return commit, "" if branch == "HEAD" else branch


def _inside(path: Path, root: Path) -> bool:
    """Whether *path*, every link on the way resolved, is still under *root*."""
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError):
        return False


def _is_plain_dir(path: Path, root: Path) -> bool:
    # ``is_symlink`` misses a Windows junction, so the resolved path must also
    # stay inside the clone.
    return not path.is_symlink() and path.is_dir() and _inside(path, root)


def _has_manifest(folder: Path, root: Path) -> bool:
    manifest = folder / APP_MANIFEST_FILENAME
    return not manifest.is_symlink() and manifest.is_file() and _inside(manifest, root)


def locate_app_dir(checkout: Path, subdirectory: tuple[str, ...]) -> Path:
    """The folder of *checkout* that holds ``app.json``.

    *subdirectory* names it. Without one: the repository root, else the one
    top-level folder holding an ``app.json``. A symlink is never followed, and any
    other link (a Windows junction) must resolve inside the clone, so the folder
    installed is always inside it.
    """
    if subdirectory:
        folder = checkout
        for part in subdirectory:
            folder = folder / part
            if not _is_plain_dir(folder, checkout):
                raise GitInstallError(
                    f"the repository has no folder {'/'.join(subdirectory)!r} "
                    "(a link is not followed)"
                )
        if not _has_manifest(folder, checkout):
            raise GitInstallError(f"{'/'.join(subdirectory)}/{APP_MANIFEST_FILENAME} not found")
        return folder
    if _has_manifest(checkout, checkout):
        return checkout
    candidates = sorted(
        child.name
        for child in checkout.iterdir()
        if child.name != ".git"
        and _is_plain_dir(child, checkout)
        and _has_manifest(child, checkout)
    )
    if len(candidates) == 1:
        return checkout / candidates[0]
    if not candidates:
        raise GitInstallError(
            f"no {APP_MANIFEST_FILENAME} at the repository root or in a top-level "
            "folder; pass --subdirectory <folder>"
        )
    raise GitInstallError(
        f"several folders hold an {APP_MANIFEST_FILENAME} ({', '.join(candidates)}); "
        "pass --subdirectory <folder>"
    )


def _manifest_name(app_dir: Path) -> str:
    """The app name *app_dir*'s ``app.json`` declares, checked as a folder name."""
    try:
        # The parse ``AppManifest.from_json_file`` uses, so a leading byte-order
        # mark reads here exactly as it does for a folder install.
        data = loads_user_json((app_dir / APP_MANIFEST_FILENAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise GitInstallError(f"{APP_MANIFEST_FILENAME} is unreadable: {exc}") from exc
    name = data.get("name") if isinstance(data, dict) else None
    if not isinstance(name, str):
        raise GitInstallError(f"{APP_MANIFEST_FILENAME} declares no app name")
    error = app_name_error(name)
    if error:
        raise GitInstallError(f"{APP_MANIFEST_FILENAME} declares an invalid name: {error}")
    return name


def _checkout_prefix(url: str) -> str:
    """``<repository name>-``: the start of a checkout folder's name, for a reader."""
    last = re.split(r"[/:]", url.rstrip("/"))[-1]
    if last.endswith(".git"):
        last = last[: -len(".git")]
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", last).strip("-")[:40]
    return f"{slug or 'app'}-"


def fetch_app_from_git(
    url: str, *, ref: str | None = None, subdirectory: str | None = None
) -> FetchedApp:
    """Clone *url* and return the folder to install, or raise :class:`GitInstallError`.

    Refuses before cloning on a bad URL, ref or subdirectory, or a host without
    git, and after cloning when the app is already installed, which the install
    would refuse anyway. Each call clones into a new folder of its own, made by
    ``mkdtemp``, and nothing here renames, replaces or deletes any other folder, so
    one install can never swap the source another is still copying. A failure
    removes only the clone this call made.
    """
    url = validate_git_url(url)
    ref = validate_ref(ref)
    parts = validate_subdirectory(subdirectory)
    if not _git_on_path():
        raise GitInstallError("installing from a git URL needs git, which is not on PATH")

    root = git_checkouts_root()
    ensure_directory(root)
    checkout = Path(tempfile.mkdtemp(prefix=_checkout_prefix(url), dir=root))
    try:
        _run_clone(url, checkout, ref)
        app_dir = locate_app_dir(checkout, parts)
        name = _manifest_name(app_dir)
        if get_app(name) is not None:
            raise GitInstallError(
                f"app {name!r} is already installed; update it with "
                f"`kirocrew app update {name}`, or uninstall it first"
            )
        commit, branch = _cloned_head(checkout)
    except BaseException:
        if checkout.exists() and not rmtree_force(checkout):
            logger.warning("could not remove the partial clone %s", checkout)
        raise
    return FetchedApp(name=name, checkout=checkout, app_dir=app_dir, commit=commit, branch=branch)
