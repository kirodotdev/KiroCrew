"""Authoritative repository, worktree, and dirty-state access for Dev Fleet."""

from __future__ import annotations

import asyncio
import errno
import json
import locale
import os
import re
import subprocess
from pathlib import Path, PurePosixPath

from kiro_crew.apps.builtins.dev_fleet import runtime
from kiro_crew.executors import subprocess_executor


def _resolve_primary_checkout(path: str) -> str:
    """Given any checkout (primary or linked worktree), return the primary
    checkout path. A linked worktree's --git-common-dir points at the
    primary's .git directory."""
    git = runtime._trusted_bin("git")
    if git is None:
        return path
    env = {k: v for k, v in os.environ.items() if runtime._is_safe_env_key(k)}
    env["PATH"] = runtime._TRUSTED_PATH
    try:
        out = subprocess.run(
            [git, "-C", path, "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            # Explicitly preserve the decoder text=True selected before this
            # call moved out of the facade; the refactor must not reinterpret
            # checkout paths under a different host locale.
            encoding=locale.getpreferredencoding(False),
            timeout=5,
            env=env,
        )
        common = out.stdout.strip()
        if out.returncode == 0 and Path(common).name == ".git":
            return str(Path(common).parent)
    except (OSError, subprocess.SubprocessError):
        pass
    return path


class RepoUnavailable(RuntimeError):
    """No usable main checkout. Base of the two ways that happens.

    Sites that deliberately degrade rather than fail catch THIS, so a new reason
    for "there is no fleet to act on" cannot slip past a handler that enumerated
    only the reasons that existed when it was written.
    """


class RepoNotConfigured(RepoUnavailable):
    """No Kiro Crew checkout could be found, so there is no fleet to manage.

    Distinct from a discovery FAILURE, where a checkout was named and git could
    not read it: nothing is broken here, the app simply has no checkout to point
    at. Callers render a setup state asking where the checkout is, rather than an
    error blaming a path.
    """


class RepoUnreadable(RepoUnavailable):
    """A checkout was named but is not one this app can manage.

    Either git cannot enumerate its worktrees, or the path is a readable
    directory that does not carry the Kiro Crew markers. Carries the same
    consequence as RepoNotConfigured for every route except ``/fleet``: the fleet
    is unknown, so no action that needs a worktree can run. Typed separately so
    the two states can be told apart — this one names the path and asks the user
    to fix it, that one asks where the checkout is.
    """


#: Set at startup when the resolved checkout does not carry the Kiro Crew markers,
#: to the message ``_repo()`` raises. Tiers 1-2 (env var, config) are taken
#: verbatim, so a configured path can be a readable directory that is not this
#: project; the message is composed on the executor at startup because it embeds
#: the config-derived source hint.
_REPO_INVALID_MSG: str | None = None


def _repo() -> str:
    """The resolved main checkout path, guaranteed usable.

    The single gate between ``MAIN_REPO`` and every git argv or path built from
    it. ``git -C ""`` does not fail — it silently runs against this process's
    working directory — and ``Path("")`` is ``Path(".")``, so an unresolved
    checkout reaching a consumer would operate on an arbitrary directory and
    return plausible results. A configured path that is a readable but unrelated
    git repository is the same hazard wearing a valid-looking path: git answers
    happily, so nothing downstream can tell. Raising here makes both states fail
    loud at every call site — including the ones that never touch a route, like
    the background refresher and sync — instead of each site carrying (or
    forgetting) its own guard. Sites that deliberately degrade catch
    ``RepoUnavailable`` and say what the degraded answer is.
    """
    if not MAIN_REPO:
        raise RepoNotConfigured("no Kiro Crew checkout found to manage")
    if _REPO_INVALID_MSG:
        raise RepoUnreadable(_REPO_INVALID_MSG)
    return MAIN_REPO


def _own_source_checkout() -> str | None:
    """The source checkout whose code this process is EXECUTING, or None.

    Derived from the location of the loaded module, so it needs no configuration
    and cannot go stale. None for a packaged or site-packages install, which is
    not a checkout at all.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if parent.name == "src" and (parent / "kiro_crew").is_dir():
            return str(parent.parent)
    return None


def _is_kirocrew_checkout(path: str) -> bool:
    """Whether *path* is a Kiro Crew source checkout. Blocking — stats only.

    Fail-closed: every marker must be present. ``.git`` alone is not enough
    because adopting an unrelated repository would list ITS worktrees and run
    Pull+Build, rebase and worktree-removal git commands inside it. ``.git`` is
    tested as a path rather than a directory since a linked worktree's is a file.
    """
    if not path:
        return False
    try:
        p = Path(path)
        return (
            (p / ".git").exists()
            and (p / "src" / "kiro_crew").is_dir()
            and (p / "pyproject.toml").is_file()
        )
    except (OSError, RuntimeError, ValueError):
        return False


# Conventional clone locations, probed in this order and ONLY as a last resort.
# A candidate is adopted solely when it passes _is_kirocrew_checkout, so an
# absent or unrelated directory is skipped rather than assumed; no candidate is
# ever named in a user-facing message, because a path the user did not choose is
# noise to them. Names are matched case-insensitively against what is on disk,
# so only the canonical spelling is listed here.
_CHECKOUT_DIR_NAMES = frozenset({"kirocrew", "kiro-crew"})
_CHECKOUT_PARENT_DIRS = (
    "",
    "repos",
    "src",
    "projects",
    "dev",
    "git",
    "code",
    "workplace",
)


def _matching_child_dirs(base: Path, wanted: frozenset[str] | set[str]) -> list[Path]:
    """Child directories of *base* whose name is in *wanted*, compared
    case-insensitively and returned as the filesystem spells them. Sorted so a
    directory holding two case-variants resolves deterministically.
    """
    try:
        return sorted(
            child for child in base.iterdir() if child.name.lower() in wanted and child.is_dir()
        )
    except (OSError, ValueError):
        return []


def _candidate_checkouts() -> list[str]:
    """Conventional clone locations under the user's home, in probe order.

    EVERY path segment comes from a directory listing rather than from joining
    the guessed spellings. On a case-insensitive filesystem a blind join succeeds
    against a differently-cased directory and yields a path that does not match
    the ones git reports for the same tree; matching on disk also finds a clone
    whose case is not in the name list, on any OS.
    """
    try:
        home = Path.home()
    except (OSError, RuntimeError):
        return []
    # One listing of home resolves every named parent to its real spelling.
    parents: dict[str, list[Path]] = {}
    for child in _matching_child_dirs(home, {p for p in _CHECKOUT_PARENT_DIRS if p}):
        parents.setdefault(child.name.lower(), []).append(child)
    found: list[str] = []
    for parent in _CHECKOUT_PARENT_DIRS:
        for base in ([home] if not parent else parents.get(parent, [])):
            found.extend(str(p) for p in _matching_child_dirs(base, _CHECKOUT_DIR_NAMES))
    return found


def _configured_main_repo() -> str:
    """The operator's explicit choice of main checkout, or ``""``.

    Env wins over config so a one-off override needs no file edit. Both are
    returned VERBATIM, with no marker test: the user named this path, so a typo
    must surface as an error against THAT path instead of being silently replaced
    by a discovered one.
    """
    explicit = os.environ.get("KIROCREW_DEVFLEET_REPO", "").strip()
    if explicit:
        return explicit
    configured = _load_dev_fleet_cfg().get("repo_path")
    return configured.strip() if isinstance(configured, str) else ""


def _repo_source_hint() -> str:
    """Where the current MAIN_REPO came from, phrased as the remedy to apply.

    Blocking (reads the config files) — executor ONLY. Being on an error path is
    not a licence to read files on the event loop: a network-backed home stalls
    every other request while this one composes its banner.
    """
    if os.environ.get("KIROCREW_DEVFLEET_REPO", "").strip():
        return "It is set by the KIROCREW_DEVFLEET_REPO environment variable."
    configured = _load_dev_fleet_cfg().get("repo_path")
    if isinstance(configured, str) and configured.strip():
        return "It is set by dev_fleet.repo_path in config.json."
    return (
        "Point Dev Fleet at your Kiro Crew checkout with the "
        "KIROCREW_DEVFLEET_REPO environment variable, or with "
        "dev_fleet.repo_path in config.json."
    )


def _discover_main_repo() -> str:
    """Resolve the main checkout, or ``""`` when there is none to find.

    Blocking (config read + stats) — executor only; ``dev_fleet_startup`` calls
    it there. Order: the operator's explicit choice, the active project
    directory, the checkout this gateway runs from, then conventional clone
    locations. Every INFERRED candidate must pass the marker test, so the fleet
    can only ever be pointed at a real Kiro Crew checkout.

    ``""`` means "no checkout found" and is deliberately not a path: inventing
    one made the out-of-the-box dashboard report a checkout as missing that the
    user had never asked for, hiding the real question of where theirs lives.
    """
    configured = _configured_main_repo()
    if configured:
        return configured
    for candidate in (
        os.environ.get("KIROCREW_PROJECT_DIR", ""),
        _own_source_checkout() or "",
        *_candidate_checkouts(),
    ):
        if _is_kirocrew_checkout(candidate):
            return candidate
    return ""


def _default_main_repo() -> str:
    """The import-time main checkout hint.

    Env and stat-only tiers of ``_discover_main_repo`` (NO subprocess and no file
    reads — this module is imported from the async route-registration path, where
    both would block the event loop). ``dev_fleet_startup`` then re-resolves via
    the full discovery chain and normalizes the result to the PRIMARY checkout,
    both on the subprocess executor.
    """
    explicit = os.environ.get("KIROCREW_DEVFLEET_REPO", "").strip()
    if explicit:
        return explicit
    for candidate in (os.environ.get("KIROCREW_PROJECT_DIR", ""), _own_source_checkout() or ""):
        if _is_kirocrew_checkout(candidate):
            return candidate
    return ""


# --- configuration ---
def _default_main_repo_state() -> tuple[str, bool]:
    """Import-time checkout hint and whether an inferred tier supplied it."""
    repo = _default_main_repo()
    explicit = os.environ.get("KIROCREW_DEVFLEET_REPO", "").strip()
    return repo, bool(repo and not explicit)


# Startup replaces this stat-only hint after the complete discovery chain runs.
MAIN_REPO, MAIN_REPO_INFERRED = _default_main_repo_state()
BASE_BRANCH = "main"

# --- upstream remote resolution (replaces hardcoded 'origin') ---
_UPSTREAM_REMOTE: str | None = None


async def _upstream_remote() -> str:
    """Resolve the configured remote for BASE_BRANCH, falling back to 'origin'.

    Uses `git config branch.<BASE_BRANCH>.remote` so renamed remotes (e.g.
    'kirocrew' instead of 'origin') are honoured automatically. Cached at
    startup via dev_fleet_startup().
    """
    global _UPSTREAM_REMOTE
    if _UPSTREAM_REMOTE is not None:
        return _UPSTREAM_REMOTE
    try:
        repo = _repo()
    except RepoUnavailable:
        # A repo that never resolved must not reach git at all — it would
        # answer for whatever tree the backend happens to sit in. Remote
        # resolution degrades to git's conventional default instead of failing.
        return "origin"
    rc, out, _ = await runtime._run_cmd(
        ["git", "-C", repo, "config", f"branch.{BASE_BRANCH}.remote"],
        timeout=5,
    )
    cand = out.strip() if rc == 0 else ""
    # Repo-writable config could smuggle an option-like value ("--exec=...")
    # that later argv interpolation (`git rebase {remote}/main`) would parse
    # as a flag. Accept only a plausible remote NAME that git itself lists.
    if cand and not cand.startswith("-") and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", cand):
        rc2, remotes, _ = await runtime._run_cmd(["git", "-C", repo, "remote"], timeout=5)
        if rc2 == 0 and cand in remotes.split():
            _UPSTREAM_REMOTE = cand
            return _UPSTREAM_REMOTE
    _UPSTREAM_REMOTE = "origin"
    return _UPSTREAM_REMOTE


# Legacy-remote fallback: a renamed project keeps old remotes (e.g. origin ->
# the pre-rename repo) whose PRs cover older worktrees. A fallback repo's
# merged verdict is trusted ONLY when that remote's BASE_BRANCH is an ANCESTOR
# of the upstream BASE_BRANCH — i.e. everything merged there is contained in
# the current main, so "merged" still means "content is shipped".
_FALLBACK_REPOS: list[str] | None = None


def _same_path(a: str, b: str) -> bool:
    # "Cannot resolve" means "not the same path", never a crash: ValueError
    # covers unresolvable operands (an embedded NUL byte in caller-supplied
    # input), OSError covers ELOOP and friends, and RuntimeError covers the
    # symlink-loop signal Path.resolve() raises on some platform/version
    # combinations instead of ELOOP.
    try:
        return Path(a).resolve() == Path(b).resolve()
    except (OSError, ValueError, RuntimeError):
        return False


# owner/repo capture, shared by identity normalization and the fallback scan.
_REPO_PATH_RE = re.compile(r"[:/]([^/]+/[^/]+?)(?:\.git)?$")


def _normalize_repo_identity(url: str) -> tuple[str, str] | None:
    """Return a ``(host, owner/repo)`` identity for a git remote URL, or None.

    Normalizes across the spellings git accepts for the same repository so two
    aliases of one repo compare equal:

    - ``https://github.com/owner/Repo.git`` and ``git@github.com:owner/repo``
      collapse to the same identity;
    - a trailing ``.git`` is stripped and the whole identity is lowercased;
    - the host is part of the identity, so ``owner/repo`` on two different
      forges stays distinct.

    Returns None when no ``owner/repo`` can be extracted.
    """
    url = url.strip()
    m = _REPO_PATH_RE.search(url)
    if not m:
        return None
    owner_repo = m.group(1).lower()
    # Host: scp-style ``user@host:owner/repo`` or a URL with a scheme.
    host = ""
    scp = re.match(r"(?:[^@/]+@)?([^/:]+):", url)
    if scp and "://" not in url:
        host = scp.group(1).lower()
    else:
        scheme = re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*://(?:[^@/]+@)?([^/:]+)", url)
        if scheme:
            host = scheme.group(1).lower()
    return (host, owner_repo)


async def _load_fallback_repos() -> None:
    global _FALLBACK_REPOS
    try:
        repo = _repo()
    except RepoUnavailable:
        # No checkout, no remotes to enumerate; the fallback list stays empty.
        return
    repos: list[str] = []
    seen: set[tuple[str, str]] = set()
    upstream = await _upstream_remote()
    # Resolve upstream's own repo identity so a remote carrying upstream's own
    # repo NAME is not mistaken for a pre-rename repo — whether it is an alias
    # of upstream (e.g. an ``origin`` left in place after the tracking remote
    # was renamed) or a fork of it under another owner. ``merge-base
    # --is-ancestor`` is trivially true for identical refs and stays true for a
    # fork until it diverges, so either would enter the fallback list under
    # upstream's own name, and the derived ``<reponame>-wt-`` prefix then flags
    # every worktree as legacy.
    upstream_identity: tuple[str, str] | None = None
    rc_up, up_url, _ = await runtime._run_cmd(
        ["git", "-C", repo, "remote", "get-url", upstream],
        timeout=5,
    )
    if rc_up == 0:
        upstream_identity = _normalize_repo_identity(up_url)
    rc, out, _err = await runtime._run_cmd(["git", "-C", repo, "remote"], timeout=5)
    if rc == 0:
        for remote in out.split():
            if remote == upstream:
                continue
            rc2, _, _ = await runtime._run_cmd(
                [
                    "git",
                    "-C",
                    repo,
                    "merge-base",
                    "--is-ancestor",
                    f"{remote}/{BASE_BRANCH}",
                    f"{upstream}/{BASE_BRANCH}",
                ],
                timeout=10,
            )
            if rc2 != 0:
                continue
            rc3, url, _ = await runtime._run_cmd(
                ["git", "-C", repo, "remote", "get-url", remote],
                timeout=5,
            )
            if rc3 != 0:
                continue
            identity = _normalize_repo_identity(url)
            if identity is None:
                continue
            # Skip a remote whose repo NAME is upstream's — an alias of upstream
            # itself, or a fork of it under another owner. Name equality is the
            # right predicate for both consumers of the fallback list: the
            # legacy-worktree prefixes are derived from the repo name alone, so
            # a same-named entry yields the ``<name>-wt-`` prefix that every
            # current-convention worktree matches, and the PR-status fallback
            # should not consult a fork either — a fork is not a pre-rename
            # repo. Name equality also subsumes identity equality, so the alias
            # case stays covered. The genuine pre-rename case — a DIFFERENTLY
            # named repo whose main is an ancestor of upstream's — still
            # qualifies.
            if upstream_identity is not None and (
                identity[1].rsplit("/", 1)[-1] == upstream_identity[1].rsplit("/", 1)[-1]
            ):
                continue
            if identity in seen:
                continue
            seen.add(identity)
            repos.append(identity[1])
    _FALLBACK_REPOS = repos


async def _load_trusted_credential_helpers() -> None:
    extra: dict[str, str] = {}
    base = int(runtime._GIT_ENV_NEUTRALIZERS["GIT_CONFIG_COUNT"])
    idx = base
    # SYSTEM scope first, then GLOBAL, mirroring git's own precedence: for a
    # multi-valued key like credential.helper the later entry wins, so the
    # operator's own global setting still overrides a machine-wide default.
    #
    # System scope is read at all because that is where macOS puts the operator's
    # helper: Xcode's Command Line Tools ship
    # `credential.helper = osxkeychain` in
    # /Library/Developer/CommandLineTools/usr/share/git-core/gitconfig, and a
    # stock install has NOTHING in global. Scanning only --global therefore left
    # the neutralizer's reset unrepaired on every stock macOS host, and `git
    # fetch` died with "could not read Username" — no tty to prompt on.
    #
    # Repo-LOCAL scope stays excluded. That is the attack surface the reset
    # exists for: a checkout Dev Fleet builds can write .git/config, and a helper
    # from there would run in the credential-bearing standard tier.
    for scope in ("--system", "--global"):
        rc, out, _err = await runtime._run_cmd(
            ["git", "config", scope, "--get-regexp", r"^credential(\..+)?\.helper$"],
            timeout=5,
        )
        # A missing system gitconfig is rc != 0 with no output — normal, not an
        # error worth surfacing.
        if rc != 0 or not out:
            continue
        for line in out.splitlines():
            key, _, val = line.partition(" ")
            if not key.endswith(".helper"):
                continue
            trusted_val = runtime._sanitize_helper_value(val.strip())
            if trusted_val is None:
                # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
                # No secret is logged: the helper VALUE is deliberately
                # withheld; only the config KEY name is recorded.
                runtime.logger.warning(
                    "dev-fleet: skipping helper with unverifiable provenance"
                    " for config key %s (%s scope)",
                    key,
                    scope.lstrip("-"),
                )
                continue
            extra[f"GIT_CONFIG_KEY_{idx}"] = key
            extra[f"GIT_CONFIG_VALUE_{idx}"] = trusted_val
            idx += 1
            if idx - base >= 9:
                break
        if idx - base >= 9:
            break
    if idx > base:
        extra["GIT_CONFIG_COUNT"] = str(idx)
    runtime._GIT_TRUSTED_HELPERS = extra


def _load_dev_fleet_cfg() -> dict:
    """Read the ``dev_fleet`` config section (config.json + local overlay),
    lazily and best-effort. Never raises; a missing file/section -> {}. Read
    directly rather than through KiroCrewConfig (a separate process owns the
    validated loader) so a purely cosmetic template needs no schema dependency
    and can never break the fleet payload."""
    section: dict = {}
    try:
        from kiro_crew.config.loader import config_dir

        base = config_dir()
    except Exception:  # noqa: BLE001
        return section
    for fname in ("config.json", "config.local.json"):
        p = base / fname
        try:
            if not p.is_file():
                continue
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(raw, dict) and isinstance(raw.get("dev_fleet"), dict):
            section.update(raw["dev_fleet"])
    return section


# --- worktree discovery via git worktree list --porcelain ---
def _parse_worktree_porcelain(raw: str) -> list[dict]:
    """Parse `git worktree list --porcelain` output into a list of dicts."""
    entries: list[dict] = []
    current: dict = {}
    for line in raw.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        if line.startswith("worktree "):
            current["path"] = line[9:]
        elif line.startswith("HEAD "):
            current["head"] = line[5:]
        elif line.startswith("branch "):
            ref = line[7:]
            current["branch"] = ref.split("refs/heads/", 1)[-1] if "refs/heads/" in ref else ref
        elif line == "detached":
            current["branch"] = None
        elif line == "prunable" or line.startswith("prunable "):
            # git flags an entry `prunable` when its checkout directory is gone
            # but the admin record survives (a `rm -rf` with no
            # `git worktree prune`). The reason text is optional.
            current["prunable"] = line[len("prunable") :].strip() or "unknown"
        elif line == "locked" or line.startswith("locked "):
            # An explicit human "do not touch this tree". `git worktree remove`
            # refuses a locked tree, and its refusal comes LAST -- after any
            # pre-removal cleanup has already run -- so every removal path has
            # to recognise the lock up front instead of discovering it too late.
            # The reason text is optional and author-controlled.
            current["locked"] = line[len("locked") :].strip() or "unknown"
    if current:
        entries.append(current)
    return entries


async def _discover_worktrees() -> list[dict]:
    """List git worktrees of MAIN_REPO."""
    # Nothing to discover when no checkout resolved; _repo() raises
    # RepoNotConfigured and the setup state is the caller's job.
    repo = _repo()
    rc, stdout, stderr = await runtime._run_cmd(
        ["git", "-C", repo, "worktree", "list", "--porcelain"], timeout=10
    )
    if rc != 0:
        # Propagate sandbox/git failures as a RuntimeError so callers can
        # surface the real reason instead of returning silent empty lists.
        raw = (stderr or stdout or "").strip()
        if "sandbox unavailable" in raw:
            # Do NOT clip to the generic git-error length here. The sandbox layer
            # puts the *remedy* (which opt-in to set, or that an EPERM is a
            # Seatbelt nesting artifact rather than a missing backend) AFTER a
            # ~180-char preamble, so a tight cap would surface the diagnosis and
            # swallow the fix. Keep a generous bound purely to stop an unbounded
            # stderr reaching the UI.
            raise RepoUnreadable(raw[: runtime._SANDBOX_ERR_MAX])  # already prefixed by _run_cmd
        if raw.startswith(runtime._UNRESOLVED_TOOL_PREFIX):
            # git never ran: the HOST has no git the resolver is willing to
            # execute. Checked before the .git probe because the probe's
            # outcome is irrelevant here — wrapping this in "worktree
            # discovery failed in <repo>" (the old behavior) sent users to
            # debug a healthy checkout (#2530). The trusted-PATH detail is
            # operator-diagnostic, so it goes to the log, not the banner.
            runtime.logger.warning("dev-fleet: %s", raw)
            raise RepoUnreadable(runtime._unresolved_tool_message("git"))
        # Every other git failure was previously swallowed into a silent [] —
        # which the UI renders as the "No worktrees found / Nothing under the
        # worktrees root yet" empty state. When MAIN_REPO is wrong that empty
        # state is a lie: the fleet is not empty, it is unreadable. Reaching here
        # means a checkout WAS named — discovery only ever adopts a path that
        # carries the Kiro Crew markers, so an unverifiable one came from the
        # operator's own env var or config — so name it and raise, and
        # api_dev_fleet_fleet's error path renders the Discovery Error banner.
        # The .git probe is a filesystem stat — on a wedged network mount it
        # can block indefinitely, and this branch is reachable precisely when
        # the checkout is unhealthy (git already failed or timed out against
        # it). Same "Blocking — executor only" convention as _is_checkout().
        loop = asyncio.get_running_loop()
        repo_is_git = await loop.run_in_executor(
            subprocess_executor(), (Path(repo) / ".git").exists
        )
        if not repo_is_git:
            # Name the mechanism that supplied the path: the remedy is to edit
            # THAT one, and a message listing both leaves the user guessing which
            # of the two they set. Resolved on the executor with the probe above —
            # it reads config files, and a network-backed home would otherwise
            # stall the gateway loop on the way to rendering an error banner.
            hint = await loop.run_in_executor(subprocess_executor(), _repo_source_hint)
            raise RepoUnreadable(
                f"main checkout not found: {repo} is missing or not a git " f"checkout. {hint}"
            )
        # The repo exists but git failed for some other reason (corrupt repo,
        # permissions): surface git's own message, redacted and bounded.
        raise RepoUnreadable(
            f"git worktree discovery failed in {repo}: "
            f"{runtime._redact(raw)[:runtime._GIT_ERR_MAX] or 'unknown git error'}"
        )
    entries = _parse_worktree_porcelain(stdout)
    # `git worktree list --porcelain` always lists the primary checkout
    # first — that is the authoritative main, regardless of whether
    # MAIN_REPO itself points at a linked worktree (it is only the
    # repository discovery hint).
    for i, e in enumerate(entries):
        e["is_main"] = i == 0
    # A `prunable` entry has no checkout on disk, so every git call against its
    # path fails and it renders as a ghost row with no branch, behind count or
    # timestamp — and no refresh ever clears it, because git keeps reporting the
    # record until `git worktree prune` runs. Drop those. The primary checkout
    # is never filtered: it anchors `is_main`, and losing it would promote a
    # linked worktree to main.
    return [e for e in entries if e.get("is_main") or not e.get("prunable")]


async def _git(git_dir: str, *args: str, timeout: int = 6, mode: str = "standard") -> str | None:
    # Repo-controlled execution vectors are neutralized centrally in
    # _run_cmd via _GIT_ENV_NEUTRALIZERS — no per-call-site flags needed.
    rc, stdout, _ = await runtime._run_cmd(
        ["git", "-C", git_dir, *args], timeout=timeout, mode=mode
    )
    return stdout.strip() if rc == 0 else None


async def _git_info(path: str) -> dict:
    info: dict = {
        "branch": None,
        "head": None,
        "head_oid": None,
        "dirty": False,
        "ahead": 0,
        "behind": 0,
        "last_updated_at": None,
    }
    info["branch"] = await _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    full_head = await _git(path, "rev-parse", "HEAD")
    info["head_oid"] = full_head
    info["head"] = full_head[:7] if full_head else None
    st = await _git(path, "status", "--porcelain")
    if st is not None:
        info["dirty"] = len(st) > 0
    remote = await _upstream_remote()
    behind = await _git(path, "rev-list", "--count", f"HEAD..{remote}/{BASE_BRANCH}")
    if behind and behind.isdigit():
        info["behind"] = int(behind)
    ct = await _git(path, "log", "-1", "--format=%ct")
    if ct and ct.isdigit():
        info["last_updated_at"] = int(ct)
    return info


async def _git_ahead(path: str) -> int | None:
    """Patch-unique local commits via git cherry."""
    remote = await _upstream_remote()
    ch = await _git(path, "cherry", f"{remote}/{BASE_BRANCH}", "HEAD", timeout=12)
    if ch is not None:
        return sum(1 for ln in ch.splitlines() if ln.startswith("+"))
    ar = await _git(path, "rev-list", "--count", f"{remote}/{BASE_BRANCH}..HEAD")
    return int(ar) if ar and ar.isdigit() else None


async def _own_commits_count(path: str) -> int | None:
    remote = await _upstream_remote()
    out = await _git(path, "rev-list", "--count", f"{remote}/{BASE_BRANCH}..HEAD")
    return int(out) if out and out.isdigit() else None


def _discard_untracked_files(worktree: str, rel_paths: list[str]) -> str | None:
    """Delete exactly the approved untracked files. None on success, else a reason.

    Deliberately NOT ``git clean``. A pathspec naming an entry whose type changed
    between consent and execution is followed recursively -- verified: an approved
    regular file ``scratch`` replaced by a directory ``scratch/`` containing an
    unapproved file loses that file, with ``-fd`` AND with a bare ``-f``, even
    when the pathspec is spelled ``:(literal)``. ``os.unlink`` cannot do that: it
    removes ONE non-directory entry and raises ``IsADirectoryError`` when the name
    now refers to a directory, so a type change is a refusal rather than a sweep.
    Having no pathspec at all also removes the pathspec-magic surface entirely.

    EVERY component of the given absolute worktree path is opened ``O_NOFOLLOW``
    from ``/`` down, and the unlink is issued relative to that directory fd.
    Opening the worktree by path in one call re-resolves its ancestors, so a
    writable ancestor swapped for a symlink would redirect the deletion before
    the walk began. ``realpath`` is deliberately NOT used first: resolution
    follows the topology as it stands NOW, so it resolves INTO a swapped ancestor
    and lands the deletion in the attacker's target -- tried and verified to
    destroy an external file, which is laundering the swap rather than refusing
    it. The price is that a worktree path containing a legitimately symlinked
    ancestor is refused; that is the same trade as the platform check below.
    Where these primitives do not exist (Windows has no ``openat``/``O_NOFOLLOW``)
    the discard is REFUSED rather than downgraded to a path-based unlink, since a
    junction swapped into an ancestor is not even reported as a link by
    ``os.path.islink``. Empty directories are left behind on purpose -- git does
    not track them, ``status``/``ls-files`` do not report them, and ``git worktree
    remove`` does not object to them (verified), so removing them would be scope
    this consent does not cover.
    """
    if not ({"O_NOFOLLOW"} <= set(dir(os)) and os.unlink in os.supports_dir_fd):
        # No openat/O_NOFOLLOW (Windows). A path-based unlink re-resolves every
        # ancestor at each step, so a directory component swapped for a symlink
        # -- or a Windows junction, which `os.path.islink` does not even report
        # as a link -- redirects the deletion outside the worktree. There is no
        # safe way to do this here, so the affordance is withdrawn rather than
        # approximated: the caller loses a button, not a file.
        return (
            "cannot discard untracked files safely on this platform (no "
            "openat/O_NOFOLLOW, so a swapped directory could redirect the "
            "deletion outside the worktree) -- clean the worktree manually, "
            "then remove it"
        )
    # Walk the GIVEN path from `/`, pinning every component with O_NOFOLLOW.
    # Opening the worktree by path in one call re-resolves its ancestors, so a
    # writable ancestor swapped for a symlink redirects the deletion before the
    # walk starts.
    #
    # Deliberately NOT `realpath` first. That was tried and it DEFEATS the guard:
    # resolution follows whatever the topology says NOW, so a swapped ancestor is
    # resolved into and the deletion lands in the attacker's target -- verified,
    # an external file was destroyed. Resolution launders the swap instead of
    # refusing it.
    #
    # The cost is that a worktree whose path genuinely contains a symlinked
    # ancestor (a linked home directory, macOS /tmp) is refused. That is the same
    # trade as the platform check above: withdraw the affordance and say so,
    # rather than approximate it. git records worktree paths as plain absolute
    # paths, so this is the uncommon case, and the caller can still clean by hand.
    root_parts = PurePosixPath(worktree).parts
    if not root_parts or root_parts[0] != "/":
        return f"refusing to discard inside a non-absolute worktree path: {worktree!r}"

    for rel in rel_paths:
        parts = PurePosixPath(rel).parts
        if (
            not parts
            or any(p in ("", ".", "..") for p in parts)
            or PurePosixPath(rel).is_absolute()
        ):
            return f"refusing to discard a path that is not worktree-relative: {rel!r}"
        dir_fds: list[int] = []
        try:
            try:
                dir_fds.append(os.open("/", os.O_RDONLY | os.O_DIRECTORY))
                walked = 0
                for comp in (*root_parts[1:], *parts[:-1]):
                    dir_fds.append(
                        os.open(
                            comp,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=dir_fds[-1],
                        )
                    )
                    walked += 1
                os.unlink(parts[-1], dir_fd=dir_fds[-1])
            except FileNotFoundError:
                if walked < len(root_parts) - 1:
                    # A component of the WORKTREE path is missing, which is not
                    # the idempotent "file already gone" case below.
                    return (
                        "cannot discard untracked files: the worktree path no "
                        "longer resolves -- nothing was discarded"
                    )
                continue
            except IsADirectoryError:
                return (
                    f"refusing to discard {rel!r}: it is now a directory, not the "
                    "file that was confirmed"
                )
            except OSError as exc:
                if walked < len(root_parts) - 1 and exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    # A component of the worktree path is a symlink. Could be a
                    # host whose home directory is linked, could be an ancestor
                    # swapped since git reported the path -- indistinguishable
                    # from here, so both are refused.
                    return (
                        "cannot discard untracked files: a directory in the "
                        "worktree's own path is a symlink, so the deletion "
                        "cannot be pinned to the checkout -- clean the worktree "
                        "manually, then remove it"
                    )
                return f"could not discard {rel!r}: {exc.strerror or exc}"
        finally:
            for fd in dir_fds:
                try:
                    os.close(fd)
                except OSError:  # pragma: no cover - defensive
                    pass
    return None


async def _real_dirty(path: str) -> bool | None:
    st = await _git(path, "status", "--porcelain")
    if st is None:
        return None
    return any(ln.strip() for ln in st.splitlines())


# Bound on the untracked paths reported to the client. The list exists so a
# human can see what a discard would destroy; past a couple of dozen entries it
# stops informing that decision and only grows the payload.
_DIRTY_PATH_SAMPLE = 20


async def _dirty_split(path: str) -> tuple[bool | None, list[str]]:
    """Classify a worktree's dirt: tracked modifications vs untracked files.

    Returns ``(tracked_dirty, untracked_paths)``.

    * ``tracked_dirty`` is True when at least one TRACKED file is modified,
      staged, deleted, renamed or unmerged, False when none is, and ``None``
      when git could not answer — which callers must treat as unverifiable,
      never as clean.
    * ``untracked_paths`` are files git considers untracked and NOT ignored, so
      build output (``.venv``, ``node_modules``, anything in ``.gitignore``)
      never counts as dirt. An empty list means "none found OR git failed" — it
      is deliberately not a promise, and the discard path treats an empty list
      as "nothing approved to discard".

    Why two commands instead of parsing one ``--porcelain`` blob: ``-uno``
    suppresses untracked entries, so anything it prints is a tracked change and
    a plain non-empty test suffices; ``ls-files --others`` prints bare paths
    with no status columns to misparse.

    The untracked half deliberately bypasses the shared ``_git`` helper, which
    strips its output and would corrupt a first or last filename carrying
    leading or trailing whitespace. These paths are not merely displayed — they
    become the ``git clean`` pathspec deciding which files a discard destroys —
    so they must survive byte-exact. A corrupted path would simply fail to
    match and abort the removal, which is safe but is a refusal nobody earned.
    """
    tracked_out = await _git(path, "status", "--porcelain", "-uno")
    tracked_dirty: bool | None = (
        None if tracked_out is None else any(ln.strip() for ln in tracked_out.splitlines())
    )
    rc, others_raw, _ = await runtime._run_cmd(
        ["git", "-C", path, "ls-files", "--others", "--exclude-standard", "-z"],
        timeout=6,
    )
    untracked = [p for p in others_raw.split("\0") if p] if rc == 0 else []
    return tracked_dirty, untracked


def _dirt_fields(tracked_dirty: bool | None, untracked: list[str]) -> dict:
    """The structured dirt description carried on a refusal or a fleet row.

    Kept separate from the human message so the client can RENDER the blocking
    files instead of parsing a sentence — a refusal that only says "uncommitted
    changes" leaves the user no way to find out what is in the way.

    Emitted paths go through ``_redact``, like every other path-ish string this
    module puts on the wire (the worktree path, the design-doc list). A filename
    is author-controlled text, so it is scrubbed on the way OUT while callers
    that need to act on the file keep the raw list from ``_dirty_split``.
    """
    return {
        "dirty_tracked": tracked_dirty,
        "dirty_untracked": len(untracked),
        "dirty_untracked_paths": [runtime._redact(p) for p in untracked[:_DIRTY_PATH_SAMPLE]],
    }


def _dirt_detail(tracked_dirty: bool | None, untracked: list[str]) -> str:
    """A short phrase naming what is dirty, appended to a refusal message.

    For callers that surface only the error string (the prune checklist's
    inline failure reason), this is the whole explanation they get, so it says
    which KIND of dirt is blocking. It deliberately never suggests forcing:
    force is refused for tracked modifications too.
    """
    if tracked_dirty is None:
        return ""
    parts = []
    if tracked_dirty:
        parts.append("tracked files are modified")
    if untracked:
        shown = ", ".join(runtime._redact(p) for p in untracked[:3])
        more = f" +{len(untracked) - 3} more" if len(untracked) > 3 else ""
        parts.append(f"{len(untracked)} untracked ({shown}{more})")
    if not parts:
        return ""
    return " -- " + "; ".join(parts)


async def _dirt_report(path: str) -> tuple[dict, str]:
    """Classify a dirty worktree for a refusal payload: fields + message tail."""
    tracked_dirty, untracked = await _dirty_split(path)
    return (
        _dirt_fields(tracked_dirty, untracked),
        _dirt_detail(tracked_dirty, untracked),
    )


def _find_worktree_sync(worktrees: list[dict], name: str) -> tuple[dict | None, str | None]:
    """Resolve a worktree by display name, rejecting ambiguous basenames."""
    matches = []
    for w in worktrees:
        wname = Path(w["path"]).name if not w.get("is_main") else BASE_BRANCH
        if wname == name:
            matches.append(w)
    if not matches:
        return None, f"worktree not found: {name}"
    if len(matches) > 1:
        paths = ", ".join(w["path"] for w in matches)
        return None, f"ambiguous worktree name {name!r} matches multiple checkouts: {paths}"
    return matches[0], None


async def _find_worktree(name: str) -> tuple[dict | None, str | None]:
    wts = await _discover_worktrees()
    return _find_worktree_sync(wts, name)


async def _valid_worktree_names() -> set[str]:
    return {
        Path(w["path"]).name if not w.get("is_main") else BASE_BRANCH
        for w in await _discover_worktrees()
    }


async def _find_worktree_by_path(path: str) -> tuple[dict | None, str | None]:
    """Resolve a discovered worktree by filesystem path.

    Reuses the same ``git worktree list`` enumeration the fleet listing uses,
    so the caller-supplied path is only ever a SELECTOR validated against the
    server's authoritative set — an arbitrary path can never be made live."""
    if not path:
        return None, "'path' must be a non-empty string"
    try:
        want = Path(path).resolve()
    except (OSError, ValueError, RuntimeError):
        return None, f"invalid path: {path!r}"
    for w in await _discover_worktrees():
        try:
            if Path(w["path"]).resolve() == want:
                return w, None
        except OSError:
            continue
    return None, f"path is not a known worktree: {path!r}"


__all__ = (
    "BASE_BRANCH",
    "MAIN_REPO",
    "MAIN_REPO_INFERRED",
    "RepoNotConfigured",
    "RepoUnavailable",
    "RepoUnreadable",
    "_CHECKOUT_DIR_NAMES",
    "_CHECKOUT_PARENT_DIRS",
    "_DIRTY_PATH_SAMPLE",
    "_FALLBACK_REPOS",
    "_REPO_INVALID_MSG",
    "_REPO_PATH_RE",
    "_UPSTREAM_REMOTE",
    "_candidate_checkouts",
    "_configured_main_repo",
    "_default_main_repo",
    "_default_main_repo_state",
    "_dirt_detail",
    "_dirt_fields",
    "_dirt_report",
    "_dirty_split",
    "_discard_untracked_files",
    "_discover_main_repo",
    "_discover_worktrees",
    "_find_worktree",
    "_find_worktree_by_path",
    "_find_worktree_sync",
    "_git",
    "_git_ahead",
    "_git_info",
    "_is_kirocrew_checkout",
    "_load_dev_fleet_cfg",
    "_load_fallback_repos",
    "_load_trusted_credential_helpers",
    "_matching_child_dirs",
    "_normalize_repo_identity",
    "_own_commits_count",
    "_own_source_checkout",
    "_parse_worktree_porcelain",
    "_real_dirty",
    "_repo",
    "_repo_source_hint",
    "_resolve_primary_checkout",
    "_same_path",
    "_upstream_remote",
    "_valid_worktree_names",
)
