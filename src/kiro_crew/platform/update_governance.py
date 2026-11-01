"""The update seam: where new code may come from, and the minimum version.

Two enterprise pins, read from the trust-root ``security_policy.json`` (see
``governance.UpdatePins``), applied at the three places KiroCrew replaces its own
code: ``POST /api/update``, ``kirocrew update``, and the unattended gateway-boot
auto-apply. This module exists so those three share one implementation and
cannot drift.

Deliberately NOT a governance archetype: a remote URL and a version number are
values the core consumes, not "is X permitted?" decisions, so they need no
``SCOPE_CATALOG`` row, no matcher, and no evaluator change.

**A pin blocks; an unresolvable pin does not.** If governance cannot be read at
all the update proceeds — refusing one would strand a host on a build that may
need a patch, and the pins are a routing constraint, not a security boundary
against a local operator who could edit the checkout directly.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import re
import subprocess
import time
from typing import Callable, Iterator

from kiro_crew import platform_compat
from kiro_crew.git_worktree_scope import worktree_probe_failure_is_empty_scope
from kiro_crew.subprocess_utf8 import utf8_path_stdout, utf8_stdout

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_SECS = 10

#: A 40- or 64-hex object id, so a fetched tip read from ``FETCH_HEAD`` is a
#: commit id before it is ever written into the checkout's tracking ref.
_OID_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


#: The ONLY branch names an unattended update may reset a checkout to.
#:
#: A literal in reviewed code, and deliberately the WHOLE decision: see
#: :func:`is_primary_branch` for why no local git ref is allowed to participate.
#: This repo's primary branch is ``main``; the other two cover internal and
#: mirror clones. Mirrors ``security._PROTECTED_BRANCHES`` by intent — the branch
#: an unattended update may reset to is exactly the branch a push must never
#: target — but is kept separate so update routing does not read a
#: security-module private.
PRIMARY_BRANCHES = frozenset(
    {
        "main",
        "mainline",
        "master",  # wokeignore:rule=master  # legacy primary in older clones
    }
)


def is_primary_branch(branch: str) -> bool:
    """Whether *branch* is a primary line an unattended update may reset to.

    Membership in :data:`PRIMARY_BRANCHES` is the entire test. That is a design
    constraint, not an omission: this gate gates the most privileged path in the
    product — a boot-time ``git reset --hard`` + ``pip install`` + ``execv`` with
    no auth and no click — and it is also the path the enterprise ``min_version``
    floor drives (``_auto_apply_update`` is what a mandatory update calls on a
    checkout). So the decision must not read any state a local process can write.

    ``refs/remotes/<remote>/HEAD`` is exactly such state: one
    ``git remote set-head`` repoints it. Consulting it fails in BOTH directions,
    and there is no ordering that fixes both —

    * Obeying it as authoritative lets a repoint aim the install at an arbitrary
      branch of the still-approved origin, so unreviewed code gets installed and
      executed. The source pin cannot catch that: the remote URL is unchanged.
    * Letting it merely narrow (accept only when it agrees) turns the same
      one-command repoint into a veto — point a ``main`` checkout's pointer at
      ``mainline`` and the host silently stops updating, including for a
      mandatory floor, stranding it below the administrator's minimum version.

    A fork whose primary line is named something else entirely therefore gets no
    unattended update, only the badge. ``kirocrew update`` and the dashboard
    apply path still serve it, and both have a human in the loop — the
    difference that makes wider trust acceptable there and not here.

    A wrong literal here fails SILENTLY — the gate returns and the host simply
    never updates — which is what a hardcoded ``!= "mainline"`` did to every
    ``main`` checkout of this repo for three months. The allowlist is the fix for
    that: it names every primary line this project's clones actually use, so no
    single name has to be guessed right.

    A detached HEAD is never primary: there is no branch to fast-forward.
    """
    return branch in PRIMARY_BRANCHES


#: Git binary override for :func:`_git_probe`, set only by
#: :func:`nested_repo_exec_driver_reason` for the duration of one probe.
_PROBE_GIT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "update_governance_probe_git", default=None
)


#: Set only by :func:`nested_repo_exec_driver_reason`: count driver keys from
#: EVERY scope git reads, not just the repository's. Its callers' host git reads
#: global config, which an agent can write, and a nested repo's own
#: ``.gitattributes`` can bind a driver declared there.
_PROBE_ALL_SCOPES: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "update_governance_probe_all_scopes", default=False
)


#: Set only by :func:`nested_repo_exec_driver_reason`: a list each probe that
#: TIMED OUT appends to. A timeout says nothing about whether the caller's own
#: git can read the same repo, so that walk refuses rather than skipping it.
_PROBE_TIMEOUTS: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "update_governance_probe_timeouts", default=None
)


#: Global/system driver keys that an in-tree ``.gitattributes`` can bind by name.
_USER_DRIVER_KEY_RE = re.compile(
    r"^(?P<a>filter|diff)\.(?P<n>.*)\.(?:process|smudge|clean|textconv|command)$",
    re.IGNORECASE,
)


def _driver_attr_and_name(key: str) -> tuple[str, str]:
    """``("filter", "lfs")`` for ``filter.lfs.clean``; the attribute name lower-cased."""
    match = _USER_DRIVER_KEY_RE.match(key)
    assert match is not None
    return match.group("a").lower(), match.group("n")


def _bound_driver_names(proj: str) -> set[tuple[str, str]] | None:
    """Every ``(filter|diff, <name>)`` the tracked files of *proj* are bound to.

    ``check-attr`` reads attributes and runs no driver. ``None`` when git cannot
    answer, which the caller treats as unreadable (a refusal).
    """
    listing = _git_probe(proj, "ls-files", "-z", decoder=utf8_path_stdout)
    if listing is None:
        return None
    names = [n for n in listing.split("\0") if n]
    if not names:
        return set()
    git = _PROBE_GIT.get() or platform_compat.trusted_git_bin()
    if git is None:
        return None
    try:
        done = subprocess.run(
            [git, "check-attr", "-z", "--stdin", "filter", "diff"],
            cwd=proj,
            input=b"\0".join(os.fsencode(n) for n in names) + b"\0",
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECS,
            env=_PROBE_ENV.get() or git_command_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    fields = utf8_stdout(done.stdout).split("\0")
    bound: set[tuple[str, str]] = set()
    # `-z` output: `<path>\0<attr>\0<value>\0` per path and attribute.
    # Every reported value counts, `set` and the empty value included: git runs
    # `filter.set.clean` for `filter=set` and `filter..clean` for `filter=`, and
    # `check-attr` prints those exactly like a bare `filter` / an empty value.
    for attr, value in zip(fields[1::3], fields[2::3]):
        if value not in ("unspecified", "unset"):
            bound.add((attr.lower(), value))
    return bound


#: Env override for :func:`_git_probe` calls that pass no ``env=``, set only by
#: :func:`repo_exec_config_reason` with ``human_transport=True``.
_PROBE_ENV: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "update_governance_probe_env", default=None
)


@contextlib.contextmanager
def _probing_with(git: str, env: dict[str, str] | None = None) -> Iterator[None]:
    """Run :func:`_git_probe` with *git* (``""``: the trusted git) and *env* inside the block."""
    token = _PROBE_GIT.set(git or None)
    env_token = _PROBE_ENV.set(env)
    try:
        yield
    finally:
        _PROBE_ENV.reset(env_token)
        _PROBE_GIT.reset(token)


def _git(proj: str, *args: str) -> str:
    """Run a read-only git command in *proj*, returning stripped stdout or ``""``."""
    out = _git_probe(proj, *args)
    return out.strip() if out is not None else ""


def _git_probe(
    proj: str,
    *args: str,
    decoder: Callable[[bytes | str | None], str] = utf8_stdout,
    env: dict[str, str] | None = None,
) -> str | None:
    """As :func:`_git`, but ``None`` when git could not answer at all.

    *env* replaces :func:`git_command_env` for a read that must see exactly what
    a given fetch will see (see :func:`resolve_remote_url`).

    Every git invocation in this module carries
    :func:`git_neutralizer_env`, so a repo-planted ``core.fsmonitor`` (or any
    other fixed-key exec vector) cannot run just because the update seam looked
    at the checkout.

    ``git`` itself is resolved through
    :func:`platform_compat.trusted_git_bin` rather than ``PATH``: a gateway's
    ``PATH`` can lead with an agent-writable directory, and a planted ``git``
    shim on THIS path chooses what the process installs and re-executes. An
    unresolvable git returns ``None`` — a refusal, not a bare-name fallback.
    """
    # A caller that runs its OWN git (not the trusted one) probes with that same
    # binary, so "git could not answer" means its call cannot run either; see
    # :func:`nested_repo_exec_driver_reason`. Everyone else gets the trusted git.
    git = _PROBE_GIT.get() or platform_compat.trusted_git_bin()
    if git is None:
        # No trustworthy git. Answer "could not determine", which every caller
        # already treats as the unsafe direction — never fall back to a bare
        # `"git"`, which is the hazard itself.
        return None
    try:
        # Bytes mode, decoded by utf8_stdout: text mode's universal-newline
        # translation rewrites every ``\r`` in git's stdout, and the RAW-stdout
        # contract at the `--worktree` classification site below requires a
        # ``\r`` inside a ``rev-parse --absolute-git-dir`` answer to survive as
        # path content (see kiro_crew.git_worktree_scope).
        done = subprocess.run(
            [git, *args],
            cwd=proj,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECS,
            env=(_PROBE_ENV.get() or git_command_env()) if env is None else env,
        )
    except subprocess.TimeoutExpired:
        timeouts = _PROBE_TIMEOUTS.get()
        if timeouts is not None:
            timeouts.append(args[0] if args else "")
        return None
    except (OSError, subprocess.SubprocessError):
        return None
    return decoder(done.stdout) if done.returncode == 0 else None


def resolve_remote_url(
    proj: str,
    *,
    fetch_env: dict[str, str] | None,
    remote: str = "",
    branch: str = "",
) -> str:
    """The URL the update fetch in *proj* will connect to (``""`` if unknown).

    *fetch_env* is the environment that fetch runs with, passed by the caller
    so the pin check and the fetch read the SAME git config: the source pin
    judges the URL that fetch will use, and no other. ``None`` means the fetch
    passes no ``env=`` and so inherits this process's environment, including
    the user's global and system config (``kirocrew update``, the dashboard
    update). The gateway's auto-update passes its :func:`git_command_env`
    result, which reads neither, so a ``~/.gitconfig`` ``url.<x>.insteadOf`` an
    agent planted cannot move what that fetch connects to and is not what the
    pin judges there either. Each caller keeps one variable for both calls.

    Pass *remote* for a FIXED remote name: the CLI and gateway paths run
    ``git fetch origin <branch>``, so they must validate ``origin`` rather than
    whatever the branch tracks. With no *remote*, ``branch.<name>.remote`` is
    resolved instead — that is the API path's bare ``git pull``, which follows the
    tracked remote. Validating a different remote than the one fetched would
    approve one source and install another.

    ``ls-remote --get-url`` (not ``remote get-url``) additionally applies
    ``url.<base>.insteadOf`` rewriting, so the value checked is the URL git
    resolves rather than the one merely written down. None of these reads
    contacts a remote. The inherited view adds the fixed-key exec pins, which do
    not change what a URL or a tracked remote resolves to.

    Returns ``""`` on any failure, which a source pin then treats as "not
    permitted" and an unpinned host ignores.
    """
    env = {**os.environ, **git_neutralizer_env()} if fetch_env is None else fetch_env

    def git(*args: str) -> str:
        out = _git_probe(proj, *args, env=env)
        return out.strip() if out is not None else ""

    if not remote:
        if not branch:
            branch = git("rev-parse", "--abbrev-ref", "HEAD")
        if not branch or branch == "HEAD":  # detached HEAD tracks nothing
            return ""
        remote = git("config", "--get", f"branch.{branch}.remote") or "origin"
    url = git("ls-remote", "--get-url", "--", remote)
    # `--get-url` echoes its argument back for an unknown remote; that is a bare
    # name, not a URL, and must not be checked as one.
    return "" if url == remote else url


def upstream_remote_and_branch(proj: str) -> tuple[str, str] | None:
    """``(remote, branch)`` the current branch of *proj* tracks, or ``None``.

    The pair ``@{u}`` resolves to, read straight from config rather than from
    the ``origin/<name>`` short form (which cannot be split back into a remote
    and a ref when either contains a ``/``). ``None`` for a detached HEAD or a
    branch with no upstream -- there is nothing to compare against, exactly the
    case the old bare ``git fetch`` + ``rev-parse @{u}`` reported as a failed
    check.
    """
    branch = _git(proj, "rev-parse", "--abbrev-ref", "HEAD")
    if not branch or branch == "HEAD":
        return None
    remote = _git(proj, "config", "--get", f"branch.{branch}.remote")
    merge = _git(proj, "config", "--get", f"branch.{branch}.merge")
    if not remote or not merge.startswith("refs/heads/"):
        return None
    return remote, merge[len("refs/heads/") :]


def _local_fetch_url(proj: str, url: str) -> str:
    """A local path *url* made absolute against *proj*; a scheme/scp URL unchanged.

    The fetch runs with ``cwd=proj`` but ``GIT_DIR`` elsewhere, so a bare local
    path has to be absolute or it resolves against the wrong base. A leading
    ``~`` is expanded, and a relative path (and a relative ``proj``) is resolved
    against the checkout -- both are supported remote spellings. A scheme URL
    (``https://``) or an scp-like ``host:path`` is left exactly as configured.
    """
    first = url.split("/", 1)[0]
    if "://" in url or ":" in first:  # scheme URL or scp-like host:path
        return url
    expanded = os.path.expanduser(url)
    if os.path.isabs(expanded):
        return expanded
    return os.path.abspath(os.path.join(os.path.abspath(proj), expanded))


#: Repo-scoped config keys whose VALUE git may execute as a program, and whose
#: KEY NAME is fixed so ``-c``/``GIT_CONFIG_*`` can pin it back to a safe value.
#:
#: The membership criterion is exactly that — "git may exec this value, and the
#: key is a literal" — and it is written down because an enumeration without one
#: misses members (``core.gitProxy``). Adding a key here is correct whenever git might exec it;
#: the cost of an unnecessary pin is nil, because none of these are values the
#: update path wants from the repository in the first place.
#:
#: Keys whose NAME is repo-chosen (``filter.<name>.smudge``,
#: ``diff.<driver>.textconv``, ``credential.<url>.helper``) cannot be pinned at
#: all — there is no name to override — so those are refused instead, by
#: :func:`repo_exec_config_reason`.
#:
#: Mirrors the neutralizer lists the app-side git callers already carry
#: (``apps/builtins/md_notebook/git_ops.py``, ``papyrus/backend/gitops.py``,
#: ``dev_fleet/server.py``). This copy exists because ``platform/`` must not
#: import from ``apps/``, and the update seam needs it at the same chokepoint.
_GIT_EXEC_NEUTRALIZERS: tuple[tuple[str, str], ...] = (
    # `git status` and `git diff` consult core.fsmonitor and SPAWN it.
    ("core.fsmonitor", "false"),
    # Any command may run hooks; a reset checks files out.
    ("core.hooksPath", os.devnull),
    ("credential.helper", ""),
    ("core.sshCommand", "ssh"),
    # A fixed-key exec vector on the `git diff` call below.
    ("diff.external", ""),
    # Prompt helpers git execs when a transport wants credentials.
    ("core.askPass", ""),
    # Executed while enumerating an alternate object store's refs.
    ("core.alternateRefsCommand", ""),
    # Server-side hook honoured for a local/file transport fetch.
    ("uploadpack.packObjectsHook", ""),
    # Paged output execs the pager. `--porcelain`/`--quiet` and a non-tty stdout
    # make this unreachable today, which is exactly why it is pinned rather than
    # reasoned about at each call site.
    ("core.pager", "cat"),
    ("core.editor", "true"),
    ("sequence.editor", "true"),
    # Reached through signature verification, which is also forced off below.
    ("gpg.program", "true"),
    # The SSH and X.509 verifiers are separate keys, reached the same way -- and
    # also by `archive`'s `export-subst` expanding `%G?` on a signed HEAD.
    ("gpg.ssh.program", "true"),
    ("gpg.x509.program", "true"),
    # Signature VERIFICATION is the trigger here: a fetched commit carrying a
    # gpgsig header would invoke gpg.program. The update never verifies
    # signatures, so the only reason git would exec it is an attacker's.
    ("merge.verifySignatures", "false"),
    ("pull.verifySignatures", "false"),
    # Local/file transports exec the config-named pack programs directly, and
    # GIT_ALLOW_PROTOCOL does not gate them (they are not a protocol). Every
    # update fetch uses the literal remote `origin`. NOT enough on its own: git
    # keeps the FIRST value of these keys, which is .git/config's, so a repo-set
    # value is refused by `_REPO_REMOTE_PACK_RE` instead.
    ("remote.origin.uploadpack", "git-upload-pack"),
    ("remote.origin.receivepack", "git-receive-pack"),
    # `ext::` remote URLs run an arbitrary command as the transport.
    ("protocol.ext.allow", "never"),
    # A fetch that moves a gitlink recurses "on-demand" into the submodule and
    # fetches it with the submodule's OWN config, whose pack programs and
    # transport keys no pin or probe here names. The update never needs it.
    ("fetch.recurseSubmodules", "false"),
)

#: Keys in :data:`_GIT_EXEC_NEUTRALIZERS` whose VALUE is a program NAME rather
#: than a literal. They are resolved to a TRUSTED ABSOLUTE PATH when the env is
#: built, because a bare name is resolved by git through ``PATH`` at exec time --
#: the same agent-writable ``PATH`` that :func:`platform_compat.trusted_git_bin`
#: exists to bypass for ``git`` itself. Pinning ``core.sshCommand`` to ``"ssh"``
#: closes the repository's value and then hands the transport to whatever ``ssh``
#: leads the gateway's ``PATH``, which is most of the hole reopened.
#:
#: An unresolvable name degrades to ``os.devnull``, which fails to exec: for this
#: path that is the right direction, since every one of these is either
#: unreachable (pager/editor/gpg -- forced off or non-tty) or a transport helper
#: whose absence should stop an unattended update rather than silently fall back
#: to an untrusted one.
#:
#: ``remote.origin.uploadpack``/``receivepack`` are deliberately NOT here. Over
#: ssh their value is sent to the SERVER as the remote command, and GitHub and
#: GitLab accept only the bare ``git-upload-pack``: an absolute local path made
#: every ssh fetch fail. For a local/file transport git resolves the bare name
#: with its own exec-path prepended to ``PATH``, so the bare name reaches git's
#: installed helper, not a planted one.
_PROGRAM_VALUED_PINS = frozenset(
    {
        "core.sshcommand",
        "core.pager",
        "core.editor",
        "sequence.editor",
        "gpg.program",
        "gpg.ssh.program",
        "gpg.x509.program",
    }
)

#: Fixed-name transport keys an update a HUMAN started leaves UNPINNED, so git
#: reads the operator's own global/system value exactly as it would at their
#: terminal: includes, ``includeIf`` (``gitdir:``, ``hasconfig:remote.*.url:``),
#: empty resets and per-URL ``credential.<url>.helper`` entries all keep working.
#: The ``credential.helper=""`` pin clears git's WHOLE helper list, per-URL
#: entries included, and re-creating the user's list from outside git kept
#: missing a case. The repository's own value of these keys is REFUSED instead
#: (:func:`repo_exec_config_reason` with ``human_transport=True``); the per-URL
#: and arbitrary-named forms already are.
_HUMAN_TRANSPORT_KEYS = frozenset({"credential.helper", "core.sshcommand"})


#: Repo config that weakens or redirects TRANSPORT TRUST for the fetch.
#:
#: `http.sslVerify=false` in the checkout's own config turns off certificate
#: verification for the update download, which together with a hostile proxy makes
#: a forged update indistinguishable from a real one; the CA / client-cert and
#: proxy keys reach the same outcome by supplying the trust material instead of
#: disabling the check.
#:
#: REFUSED rather than pinned because the per-URL spellings
#: (``http.<url>.sslVerify``, ``http.<url>.proxy``) are repo-NAMED -- there is no
#: key to override, the same reason ``credential.<url>.helper`` is refused. The
#: bare forms are folded in here too so the two spellings cannot diverge.
_REPO_TRANSPORT_TRUST_RE = re.compile(
    r"^http\.(?:.+\.)?(?:"
    r"sslverify|sslcainfo|sslcapath|sslcert|sslkey|sslbackend"
    r"|proxy|proxyauthmethod|proxysslcert|proxysslkey|proxysslcainfo"
    r")$",
    re.IGNORECASE,
)


#: Repo-NAMED keys that redirect where the update FETCHES FROM. The base is
#: repo-chosen, so there is no key name to override -- refused, like the
#: arbitrary-named exec drivers.
#:
#: Verified, and it is the reason an "just fetch the validated URL" fix is not
#: sufficient on its own: with ``url.<evil>.insteadOf <honest>`` in repo config, a
#: fetch passed the honest URL EXPLICITLY still delivered the attacker's commit.
#: The rewrite happens below the URL argument, so the only way to trust the URL
#: that was validated is for no rewrite rule to exist.
_REPO_URL_REWRITE_RE = re.compile(
    r"^url\..*\.(?:insteadof|pushinsteadof)$",
    re.IGNORECASE,
)


#: Per-remote pack programs a fetch (or push) execs for a local/ssh transport.
#: REFUSED, not pinned: git honours the FIRST ``uploadpack`` it reads and only
#: warns "more than one uploadpack given, using the first", and repo config is read
#: before the command-scope ``GIT_CONFIG_*`` pin -- so the
#: ``remote.origin.uploadpack`` pin in :data:`_GIT_EXEC_NEUTRALIZERS` loses to a
#: repo value, the same silent loss as ``core.gitProxy`` (reproduced, git 2.55).
#: Any remote name, because a bare ``git fetch`` follows the tracked remote.
_REPO_REMOTE_PACK_RE = re.compile(
    r"^remote\..*\.(?:uploadpack|receivepack)$",
    re.IGNORECASE,
)


#: Fixed-NAME keys that do not execute anything but WIDEN THE BLAST RADIUS of
#: the destructive step. Pinned through the same ``GIT_CONFIG_*`` chokepoint as
#: the exec vectors, but kept in a separate list because the membership criterion
#: is different and both are worth keeping checkable: an entry belongs here when
#: "git execs nothing, but the key makes `reset --hard` touch more than the
#: superproject work tree".
#:
#: ``submodule.recurse``: with it enabled, `git reset --hard` recurses into
#: submodules and discards uncommitted submodule work. That is not caught by the
#: work-tree refusal one level up, because `submodule.<name>.ignore=all` (or
#: `diff.ignoreSubmodules`) makes `git status --porcelain` report a COMPLETELY
#: CLEAN tree while the submodule holds uncommitted edits — verified: status
#: empty, edit destroyed by the reset. Pinned rather than refused because the pin
#: was verified to WORK (with `submodule.recurse=false` supplied through
#: ``GIT_CONFIG_*`` the submodule edit survived), which is the distinction
#: ``core.worktree`` and ``core.gitProxy`` failed.
_GIT_BLAST_RADIUS_PINS: tuple[tuple[str, str], ...] = (("submodule.recurse", "false"),)


#: Fixed-NAME keys that still cannot be closed by pinning, so they are refused
#: like the arbitrary-named drivers below.
#:
#: The shared property is MULTI-VALUEDNESS: git reads these as a list and acts on
#: the repository's entry even when a higher-priority scope supplies another, so
#: a ``GIT_CONFIG_*`` pin reports success while changing nothing. A pin left in
#: place for such a key is worse than no pin, because it asserts the hazard is
#: handled where it is not — which is why membership here is decided by
#: reproducing the pin's failure, not by reasoning about it.
#:
#: ``core.gitProxy``: with ``core.gitProxy=""`` supplied through ``GIT_CONFIG_*``,
#: ``git config --get`` reports the empty value and the repository's proxy program
#: STILL EXECUTES on a ``git://`` fetch. Same shape as ``core.worktree``: an
#: apparently-applied pin that silently loses.
#:
#: ``gc.recentObjectsHook``: documented as executed "using the shell", and
#: explicitly multi-valued ("Multiple hooks are supported"). With an empty pin
#: applied, ``git config --get-all`` still lists the repository's own value, so
#: the pin cannot suppress it. Unlike ``core.gitProxy`` the *exec* was NOT
#: reproduced — on git 2.50.1 no ``gc``/``repack``/``prune`` invocation consulted
#: the hook (a deliberately failing hook failed nothing). It is refused on the
#: :data:`_GIT_EXEC_NEUTRALIZERS` membership criterion — git may exec this value
#: and the key is a literal — where an unnecessary entry costs nothing and a
#: missing one is how ``core.gitProxy`` was overlooked. Refusal rather than a pin
#: because the pin is the mechanism that was shown not to work.
_REPO_UNPINNABLE_KEYS = frozenset({"core.gitproxy", "gc.recentobjectshook"})


#: Repo-scoped keys naming an arbitrary-named driver program. Refused, not
#: neutralized. Mirrors ``dashboard/handlers/worktree._FILTER_KEY_RE``, widened
#: with the ``textconv`` cousin that ``git diff`` reaches;
#: ``test_governance_updates`` asserts the two stay in agreement.
#: Every repo-chosen NAME is matched with ``.*``, not ``.+``: git accepts an
#: EMPTY subsection (``[filter ""]``, key ``filter..clean``) and binds it with
#: ``filter=`` in ``.gitattributes`` (reproduced on git 2.47: the clean ran).
_REPO_EXEC_DRIVER_RE = re.compile(
    r"^(?:filter\.(?P<f>.*)\.(?:process|smudge|clean)"
    # `textconv` converts a blob to text; `command` REPLACES the whole diff with
    # an external program. Both are repo-named and both are reached by the
    # `git diff` this path runs, so refusing only one left the other open.
    r"|diff\.(?P<d>.*)\.(?:textconv|command)"
    # `credential.<url>.helper` is per-URL and so repo-named: pinning the bare
    # `credential.helper` does not reach it.
    r"|credential\.(?P<c>.*)\.helper"
    # `remote.<name>.vcs` names a TRANSPORT helper: git execs `git-remote-<value>`,
    # resolved through PATH, so the fetch itself runs a repo-chosen program.
    # Verified, and verified to be unpinnable in the useful sense: an empty pin
    # does suppress the repo's helper, but git then treats "" as a helper name and
    # every fetch dies with `remote helper '' aborted session` -- so pinning would
    # disable the update path instead of protecting it.
    r"|remote\.(?P<r>.*)\.vcs"
    # `hook.<name>.command` (git 2.54+) is a hook defined in config. The name is the
    # repository's choice and `core.hooksPath` does not reach it, so no pin covers it.
    # `fetch` and `reset` fire `reference-transaction` and `post-index-change`.
    r"|hook\.(?P<h>.*)\.(?:command|event))$",
    re.IGNORECASE,
)

#: Returned when a config scope could not be read at all. An unreadable scope
#: cannot be PROVEN driver-free, so it refuses rather than assuming the best.
_EXEC_CONFIG_UNREADABLE = "unreadable git config"


#: Environment variables that relocate what a git command OPERATES ON, as
#: opposed to what it may execute. An exported ``GIT_DIR`` in the gateway's own
#: environment points every call below at unrelated metadata while ``cwd`` still
#: says ``proj`` — so the update would read one repository's refs and reset
#: another tree. They are STRIPPED rather than pinned: the correct value is
#: "whatever git discovers from ``cwd``", which is expressed by their absence,
#: and ``GIT_WORK_TREE`` in particular cannot be set without ``GIT_DIR``.
#:
#: This is the environment twin of the ``core.worktree`` redirect that
#: :func:`repo_exec_config_reason` refuses. Closing only the config half left
#: the same redirect reachable through the process environment.
_GIT_LOCATION_VARS = frozenset(
    {
        "GIT_DIR",
        "GIT_COMMON_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_NAMESPACE",
    }
)


def git_command_env(*, human_transport: bool = False) -> dict[str, str]:
    """A complete environment for running git against a discovered checkout.

    The one env a caller should use. It is built rather than merged because the
    hazard here is a variable that must be ABSENT, and merging a neutralizer dict
    over ``os.environ`` can only add or overwrite keys — an inherited
    ``GIT_DIR`` would survive that and silently retarget the command.

    Combines the exec-vector pins from :func:`git_neutralizer_env` with the
    removal of every :data:`_GIT_LOCATION_VARS` entry, and with
    :func:`_git_home_config_isolation_env`, which stops git reading the user's
    global and system config and attributes files.

    *human_transport* builds the env for the FETCH of an update a human started
    (``kirocrew update``, the dashboard button). Those fetches read the operator's
    global config on main (their credential helper, ssh command and proxy), so
    the home-config isolation is not applied and :data:`_HUMAN_TRANSPORT_KEYS` are
    left unpinned; every other repo-level pin still applies. Such a caller MUST
    also probe with ``repo_exec_config_reason(..., human_transport=True)``, which
    refuses the repository's own value of those keys, and pass this same env to
    :func:`resolve_remote_url` so the URL judged is the URL fetched. Every other
    git call of that update uses the default, isolated env.
    """
    env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_VARS}
    env.update(git_neutralizer_env(skip=_HUMAN_TRANSPORT_KEYS if human_transport else frozenset()))
    if not human_transport:
        env.update(_git_home_config_isolation_env(env))
        # A partial clone fetches a missing object on demand, through the
        # checkout's own remote config, from inside any read (`show`, `diff`,
        # `reset`). That is a fetch the isolated update fetch never sees, so a
        # `remote.<name>.uploadpack` raced in after the preflight would run.
        # Missing objects make the call fail instead (git 2.45+).
        env["GIT_NO_LAZY_FETCH"] = "1"
        # A `<helper>::<url>` remote runs an arbitrary program as the transport;
        # only git's built-in transports may serve the call.
        env["GIT_ALLOW_PROTOCOL"] = "file:git:ssh:http:https"
        # `core.gitProxy` is multi-valued, so no `GIT_CONFIG_*` pin closes the
        # repository's value; an empty `GIT_PROXY_COMMAND` is read instead.
        env["GIT_PROXY_COMMAND"] = ""
    # A replace ref (`refs/replace/<oid>`) makes git serve DIFFERENT content for
    # a given object id, transparently and everywhere. That defeats the reason
    # the update path pins its reset target to an OID at all: "an OID cannot
    # move" is true of the id and false of what the id resolves to. Verified: with
    # `git replace <good> <evil>` in place, `git reset --hard <good>` checks out
    # the EVIL tree; with this variable set it checks out the good one.
    #
    # It cannot be handled by stripping, the way the location variables are:
    # replace refs live in the repository, not the environment, so the safe state
    # is this explicit opt-out being PRESENT.
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    return env


def git_neutralizer_env(*, skip: frozenset[str] = frozenset()) -> dict[str, str]:
    """Environment pinning every fixed-key git exec vector to a safe value.

    ``GIT_CONFIG_COUNT``/``KEY``/``VALUE`` carry the same precedence as ``git
    -c``, so these beat the repository's own ``.git/config``. Returned as
    environment rather than per-call flags so one chokepoint covers every
    invocation in a sequence and a later-added command cannot forget it.

    Covers the fixed-key exec vectors (:data:`_GIT_EXEC_NEUTRALIZERS`) AND the
    blast-radius keys (:data:`_GIT_BLAST_RADIUS_PINS`), which exec nothing but
    widen what the reset touches. A redirected work tree is a third hazard —
    data loss with nothing executed — and cannot be closed here:
    ``core.worktree`` supplied through ``-c``/``GIT_CONFIG_*`` is deliberately
    ignored by git (verified: a repo-set value still won), and the
    ``GIT_WORK_TREE`` that does override it is refused without a matching
    ``GIT_DIR``. It is therefore REFUSED instead, by
    :func:`repo_exec_config_reason`.

    Callers MUST merge this over ``os.environ`` rather than passing it alone: a
    bare env would drop ``PATH``/``HOME`` and git would not run.
    """
    # *skip* (lower-cased key names) leaves those keys to git's normal reading;
    # see :data:`_HUMAN_TRANSPORT_KEYS` for the one caller.
    pins = tuple(
        pin for pin in _GIT_EXEC_NEUTRALIZERS + _GIT_BLAST_RADIUS_PINS if pin[0].lower() not in skip
    )
    env: dict[str, str] = {"GIT_CONFIG_COUNT": str(len(pins))}
    for index, (key, value) in enumerate(pins):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        if key.lower() in _PROGRAM_VALUED_PINS:
            # A bare program name is resolved by git through PATH at exec time,
            # so pinning the key without pinning the PATH lookup only moves the
            # hazard. Resolved here rather than at import so the lookup is not
            # cached across a long-lived gateway.
            resolved = platform_compat.trusted_system_bin(value)
            value = resolved if resolved is not None else os.devnull
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def _git_home_config_isolation_env(env: dict[str, str]) -> dict[str, str]:
    """Entries that keep the user's global and system git files out of the call.

    A sandboxed agent runs as the gateway's user and can write that user's home,
    so ``~/.gitconfig``, ``$XDG_CONFIG_HOME/git/config``, the default attributes
    file ``~/.config/git/attributes``, and (on a Homebrew git) the system
    ``$(prefix)/etc/gitconfig`` and ``gitattributes`` are all agent-writable.
    Git reads them for EVERY repository. The fixed-key pins already beat them,
    but a repo-NAMED driver cannot be pinned: ``filter.x.smudge`` in global
    config plus ``* filter=x`` in the global attributes file runs a program on
    the update's ``reset --hard``, and ``url.<x>.insteadOf``, ``include.path``,
    ``http.proxy`` and the multi-valued ``core.gitProxy`` reach the fetch the
    same way. :func:`repo_exec_config_reason` only probes repository scopes, so
    these scopes are not read at all instead of being probed.

    * ``GIT_CONFIG_GLOBAL`` at the null device replaces BOTH ``~/.gitconfig``
      and the XDG file, and overrides an inherited ``GIT_CONFIG_GLOBAL``.
    * ``GIT_CONFIG_NOSYSTEM`` drops the system file, whatever an inherited
      ``GIT_CONFIG_SYSTEM`` names.
    * ``core.attributesFile`` is pinned to the null device because git's
      default attributes path does not come from config, so dropping global
      config does not drop it. ``GIT_ATTR_NOSYSTEM`` drops the system one.

    The system files are kept on Windows. Git for Windows stores settings it
    needs there (``http.sslBackend``/``http.sslCAInfo``, ``core.autocrlf``,
    ``core.longpaths``) in its system gitconfig under ``Program Files``, which a
    standard install leaves admin-only. Dropping it would fail the update's TLS
    fetch behind a store-only corporate CA and make a CRLF checkout read as
    modified. A per-user Git for Windows install leaves that file user-writable;
    that residual is accepted there.

    Appended after *env*'s existing ``GIT_CONFIG_*`` entries so their indices do
    not move. ``safe.directory`` is carried over (see
    :func:`_protected_safe_directories`). What is lost: other settings a user
    keeps in global config (an ``http.proxy``, a custom ``core.excludesFile``,
    and a ``user.name``/``user.email`` a commit would need). No caller of
    :func:`git_command_env` commits, and the update path wants none of the rest
    from a file the agent can write; a proxy still works through
    ``https_proxy``, which git reads from the environment.
    """
    pins = [("core.attributesFile", os.devnull)]
    pins += [("safe.directory", value) for value in _protected_safe_directories()]
    count = int(env.get("GIT_CONFIG_COUNT") or 0)
    out = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_COUNT": str(count + len(pins))}
    if not platform_compat.IS_WINDOWS:
        out["GIT_CONFIG_NOSYSTEM"] = "1"
        out["GIT_ATTR_NOSYSTEM"] = "1"
    for offset, (key, value) in enumerate(pins):
        out[f"GIT_CONFIG_KEY_{count + offset}"] = key
        out[f"GIT_CONFIG_VALUE_{count + offset}"] = value
    return out


def _protected_safe_directories() -> list[str]:
    """The ``safe.directory`` values system and global config hold, in git's order.

    Git honours ``safe.directory`` only from system, global and command-line
    config. An operator whose checkout has another owner (a container running as
    root over a bind-mounted clone) allows it there; with those files dropped,
    every update git call would fail with "dubious ownership". Forwarding the
    values through command-line config keeps that working.

    Reading them is safe. ``git config --get-all`` only reads files and runs
    nothing, and a ``safe.directory`` value is a path, not a program. An agent
    that plants ``safe.directory=*`` gains nothing new: it could do so before
    this isolation existed, and the repository's own exec vectors are pinned or
    refused either way. A read that fails forwards nothing, so git refuses the
    checkout instead of trusting it.

    Reused for :data:`_SAFE_DIRECTORY_TTL_SECS` per set of variables that move
    those files, because every :func:`git_command_env` call lands here and some
    run on the gateway's event loop.
    """
    git = platform_compat.trusted_git_bin()
    if git is None:
        return []
    key = (git, *(os.environ.get(name) for name in _SAFE_DIRECTORY_CACHE_VARS))
    now = time.monotonic()
    cached = _safe_directory_cache.get(key)
    if cached is not None and now - cached[0] < _SAFE_DIRECTORY_TTL_SECS:
        return list(cached[1])
    values = _read_protected_safe_directories(git)
    _safe_directory_cache.clear()
    _safe_directory_cache[key] = (now, tuple(values))
    return values


#: How long a ``safe.directory`` read is reused. An operator who adds one sees
#: it applied within this window.
_SAFE_DIRECTORY_TTL_SECS = 60.0

#: The variables that decide which system and global files git reads.
_SAFE_DIRECTORY_CACHE_VARS = (
    "HOME",
    "USERPROFILE",
    "XDG_CONFIG_HOME",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_NOSYSTEM",
)

_safe_directory_cache: dict[tuple[str | None, ...], tuple[float, tuple[str, ...]]] = {}


def _read_protected_safe_directories(git: str) -> list[str]:
    """Read the system and global ``safe.directory`` values with *git*."""
    env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_VARS}
    values: list[str] = []
    # `git config --system` reads the system file even under an inherited
    # GIT_CONFIG_NOSYSTEM, while every other git call would skip it.
    nosystem = os.environ.get("GIT_CONFIG_NOSYSTEM", "").strip().lower()
    skip_system = nosystem not in ("", "0", "false", "no", "off")
    scopes = ("--global",) if skip_system else ("--system", "--global")
    for scope in scopes:
        try:
            done = subprocess.run(
                # `--includes`: a scoped read does not follow `include.path`
                # by default, and git itself does. `-z`: a value is a path.
                [git, "config", scope, "--includes", "-z", "--get-all", "safe.directory"],
                cwd=os.path.abspath(os.sep),
                capture_output=True,
                timeout=_GIT_TIMEOUT_SECS,
                env=env,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        # rc 1 is "key not set"; any other non-zero is an unreadable scope.
        if done.returncode == 0:
            values.extend(utf8_path_stdout(done.stdout).split("\0")[:-1])
    return values


def _same_dir(a: str, b: str) -> bool:
    """Whether two paths name the same directory, symlinks and case resolved.

    ``os.path.realpath`` on both sides so a symlinked checkout (this repo is
    reached through one) does not read as a redirect, and ``normcase`` so a
    Windows drive-letter or case difference does not either.
    """
    if not a or not b:
        return False
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def repo_exec_config_reason(proj: str, *, git: str = "", human_transport: bool = False) -> str:
    """Why *proj* must not be driven unattended, or ``""`` when it is clean.

    *git* is the binary the caller's own git commands run (default: the trusted
    git); the probe runs that same one, so a host whose git sits outside the
    trusted directories gets an answer about the git it actually uses rather than
    "unreadable". *human_transport* pairs with ``git_command_env(human_transport=
    True)``: that env leaves :data:`_HUMAN_TRANSPORT_KEYS` unpinned, so the
    repository's own value of those keys is refused here instead.

    The rules are documented on :func:`_repo_exec_config_reason`.
    """
    # With *human_transport* the probe reads config under the FETCH's own view
    # (global config visible). A repo-local `includeIf "hasconfig:remote.*.url:..."`
    # can match only on a URL from global config, so an isolated probe would miss
    # the keys it pulls in while the fetch loads them.
    probe_env = git_command_env(human_transport=True) if human_transport else None
    with _probing_with(git, probe_env):
        return _repo_exec_config_reason(proj, human_transport=human_transport)


def _repo_exec_config_reason(proj: str, *, human_transport: bool) -> str:
    """Why *proj* must not be driven unattended, or ``""`` when it is clean.

    :data:`_GIT_EXEC_NEUTRALIZERS` closes the keys whose NAME is fixed. A
    content filter or a textconv driver is named by the repository
    (``filter.evil.smudge``), so there is no key to pin — the operation is
    refused instead, exactly as ``worktree._checkout_filter`` refuses a
    worktree add.

    Two scopes are probed, and both details are load-bearing (see that
    function's docstring, where each was verified empirically):

    * ``--worktree`` as well as ``--local``, because a ``--local`` listing does
      NOT report worktree-scoped keys, so a repo with
      ``extensions.worktreeConfig=true`` hides a driver from a ``--local`` probe.
    * ``--includes`` on both, because for a SPECIFIC scope query git defaults
      include-following off, so a driver reached via ``include.path`` is
      invisible to the probe yet still resolves when the command runs.

    Global and system config are not probed. A command run under
    :func:`git_command_env` does not read them at all (see
    :func:`_git_home_config_isolation_env`); host git run without that env still
    does, and moving those callers onto it is the host-git sweep's job.

    A ``commondir`` redirect is checked FIRST, because it moves the very scope
    the listings below read: a ``commondir`` file inside a plain checkout's own
    ``.git`` directory points git's COMMON git dir -- the source of ``--local``
    config and of hooks -- at wherever it names. A sandboxed agent can CREATE
    that file even under a bind seal (a seal only covers files that already
    exist), so without this the scope reads below would be the attacker's own
    config. A legitimate LINKED worktree also carries ``commondir``, but its
    ``.git`` is a FILE pointing at the real git dir, never a directory, so the
    redirect is exactly a ``commondir`` beside a ``.git`` DIRECTORY.
    """
    git_dir = os.path.join(proj, ".git")
    if os.path.isdir(git_dir):
        # The file's mere presence is the tell, read structurally so the refusal
        # does not depend on a git version honoring (or erroring on) it.
        if os.path.exists(os.path.join(git_dir, "commondir")):
            return "repository redirects its common git dir via .git/commondir"
        # Belt and suspenders for a redirect reached another way: git's own
        # resolved common dir must be this `.git`.
        common = _git_probe(proj, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if common is None:
            return _EXEC_CONFIG_UNREADABLE
        if not _same_dir(common.strip(), git_dir):
            return f"common git dir is redirected to {common.strip()[:120]}"
    try:
        keys = _repo_scoped_config_keys(proj)
    except _NestedRepoLimit:
        return _NESTED_REPO_LIMIT
    if keys is None:
        return _EXEC_CONFIG_UNREADABLE
    for key in keys:
        driver = _exec_driver_key_reason(key)
        if driver:
            return driver
        # A URL rewrite redirects the FETCH itself, and applies even when the
        # URL is passed explicitly on the command line, so validating the
        # remote's URL cannot survive one being configured.
        if _REPO_URL_REWRITE_RE.match(key):
            return f"repository rewrites remote URLs via {_shown_key(key)}"
        # A pack program the fetch execs; the env pin does not win over it.
        if _REPO_REMOTE_PACK_RE.match(key):
            return f"repository declares {_shown_key(key)}"
        # Left unpinned for a human-run update so the operator's own value
        # applies; the repository's value is therefore refused here.
        if human_transport and key.lower() in _HUMAN_TRANSPORT_KEYS:
            return f"repository declares {_shown_key(key)}"
        # Transport trust: weakening TLS verification or routing the fetch
        # through a repo-supplied proxy makes a forged update look genuine.
        if _REPO_TRANSPORT_TRUST_RE.match(key):
            return f"repository overrides transport trust via {_shown_key(key)}"

    # A redirected work tree executes nothing, so no exec-key pin touches it —
    # but `git reset --hard` would overwrite matching files in the OTHER
    # directory. Ask git where the tree actually resolves rather than parsing
    # `core.worktree`: that catches a relative value, a worktree-scoped one, and
    # one reached through `include.path` with a single probe. A legitimate linked
    # worktree resolves to the directory being operated on, so it is unaffected.
    toplevel = _git_probe(proj, "rev-parse", "--show-toplevel")
    if toplevel is None:
        return "cannot resolve the work tree"
    if not _same_dir(toplevel.strip(), proj):
        return f"work tree is redirected to {toplevel.strip()[:120]}"
    return ""


def repo_exec_driver_reason(proj: str, *, git: str = "") -> str:
    """Why a READ-ONLY host-side git call in *proj* could run a repo-named program.

    *git* is the binary the caller runs, as for :func:`repo_exec_config_reason`.
    The rules are documented on :func:`_repo_exec_driver_reason`.
    """
    with _probing_with(git):
        return _repo_exec_driver_reason(proj)


def _repo_exec_driver_reason(proj: str) -> str:
    """Why a READ-ONLY host-side git call in *proj* could run a repo-named program.

    ``""`` when the repository's own config names no driver git could execute.
    The narrow sibling of :func:`repo_exec_config_reason` for callers that only
    READ a tree (a ``status``, a worktree ``diff``, an ``archive``) and never
    fetch or reset it: the URL-rewrite, transport-trust and work-tree-redirect
    refusals protect a fetch and a reset, and refusing a plain diff over them
    would break repositories that legitimately carry a ``url.<base>.insteadOf``.

    What it does refuse is exactly what :func:`git_command_env` cannot pin: a
    driver whose key NAME the repository chooses (``filter.<name>.clean``,
    ``diff.<name>.textconv``, ``hook.<name>.command``, ...) and the multi-valued
    keys a pin does not override. Measured on git 2.47: with the env pins in
    place, ``status``, a worktree ``diff`` and ``archive`` still execute a
    planted ``filter.<name>.clean``/``smudge``.

    *proj* may be any directory inside the work tree. An unreadable config is a
    refusal, for the reason :func:`repo_exec_config_reason` gives.
    """
    try:
        keys = _repo_scoped_config_keys(proj)
    except _NestedRepoLimit:
        return _NESTED_REPO_LIMIT
    # The transport keys (`credential.<url>.helper`, `remote.<n>.vcs`,
    # `core.gitProxy`) stay refused here too: in a partial clone a read-only
    # `archive`/`diff`/`show` lazy-fetches a missing blob over the transport.
    return _driver_reason_of(keys, unreadable=_EXEC_CONFIG_UNREADABLE)


def nested_repo_exec_driver_reason(proj: str, *, git: str = "") -> str:
    """As :func:`repo_exec_driver_reason`, for the NESTED repositories under *proj* only.

    For a caller that already vets *proj*'s own config another way (the
    auto-improvement clone's unsafe-key probe and its ``.git/info/attributes``
    pin) and needs only what that cannot reach: the config of an initialized
    nested repo the index records as a gitlink, whose drivers the child git that
    ``add``/``status``/``diff`` start there will run.

    *git* is the binary the CALLER runs (default: the trusted git). The probe runs
    that same binary, which is what makes the next rule sound: a TOP-LEVEL index
    git cannot read answers ``""`` here, unlike the sibling probes, because the
    caller's own git reads the same index with the same binary, so it fails the
    same way and starts no child at all. A NESTED repo that cannot be fully probed
    refuses (an unreadable or deleted nested config does not stop the child git,
    which still runs a global driver the nested attributes bind), as do a probe
    that timed out and a walk past its bounds.
    """
    # Read under the view the CALLER's git has: git_safety's host calls do not
    # isolate global config, so an include conditioned on it must be visible here.
    scopes_token = _PROBE_ALL_SCOPES.set(True)
    timeouts: list[str] = []
    timeouts_token = _PROBE_TIMEOUTS.set(timeouts)
    try:
        # The global-visible view its callers have, with the attributes-file pin
        # they also carry (`GIT_SAFE_CONFIG`), so a global attributes file binds
        # nothing here that it cannot bind in the real call.
        probe_env = git_command_env(human_transport=True)
        count = int(probe_env.get("GIT_CONFIG_COUNT") or 0)
        probe_env[f"GIT_CONFIG_KEY_{count}"] = "core.attributesFile"
        probe_env[f"GIT_CONFIG_VALUE_{count}"] = os.devnull
        probe_env["GIT_CONFIG_COUNT"] = str(count + 1)
        with _probing_with(git, probe_env):
            try:
                keys = _nested_repo_config_keys(proj, _skip_unreadable=True)
            except _NestedRepoLimit:
                return _NESTED_REPO_LIMIT
            except _NestedProbeIncomplete:
                return _NESTED_REPO_INCOMPLETE
    finally:
        _PROBE_TIMEOUTS.reset(timeouts_token)
        _PROBE_ALL_SCOPES.reset(scopes_token)
    # A probe that timed out is not git failing to read the repo, which is the
    # only failure the caller's own git is sure to share; refuse rather than skip.
    if timeouts:
        return _NESTED_REPO_INCOMPLETE
    return _driver_reason_of(keys, unreadable="")


def _driver_reason_of(keys: list[str] | None, *, unreadable: str) -> str:
    """The first exec-driver refusal among *keys*; *unreadable* when ``None``."""
    if keys is None:
        return unreadable
    for key in keys:
        driver = _exec_driver_key_reason(key)
        if driver:
            return driver
    return ""


#: A URL's userinfo (`scheme://user:token@`) or query/fragment inside a config key's
#: subsection; both can carry a credential.
_KEY_URL_USERINFO_RE = re.compile(r"(?<=://)[^/\s]*@")
_KEY_URL_QUERY_RE = re.compile(r"[?#][^\s]*?(?=\.[A-Za-z][A-Za-z0-9-]*$|$)")


def _shown_key(key: str) -> str:
    """*key* as a refusal reason may show it: URL credentials removed, then capped.

    Reasons reach the gateway log and HTTP bodies, and a URL-bearing subsection
    (`url.https://user:token@host/.insteadOf`, `credential.<url>.helper`,
    `http.<url>.proxy`) can embed a credential. Redacted BEFORE truncating, so a
    cut cannot leave half a token behind.
    """
    shown = _KEY_URL_USERINFO_RE.sub("<redacted>@", key)
    shown = _KEY_URL_QUERY_RE.sub("?<redacted>", shown)
    return shown[:120]


def _exec_driver_key_reason(key: str) -> str:
    """The refusal for one repo-scoped config *key* naming an exec driver, else ``""``.

    ``remote.<n>.uploadpack``/``receivepack`` count too: in a partial clone a
    read-only ``show``/``diff``/``archive`` lazy-fetches a missing blob from the
    promisor remote, and git runs the FIRST uploadpack it reads -- the
    repository's -- so no env pin covers it.
    """
    if (
        _REPO_EXEC_DRIVER_RE.match(key)
        or _REPO_REMOTE_PACK_RE.match(key)
        or key.lower() in _REPO_UNPINNABLE_KEYS
    ):
        return f"repository declares {_shown_key(key)}"
    return ""


#: Bounds on the nested-repository walk in :func:`_repo_scoped_config_keys`. A tree
#: past either is refused (:data:`_NESTED_REPO_LIMIT`), never answered as clean.
_MAX_NESTED_REPO_DEPTH = 8
_MAX_NESTED_REPOS = 256
_NESTED_REPO_LIMIT = "too many nested git repositories to check"


class _NestedRepoLimit(Exception):
    """The nested-repository walk passed one of its bounds."""


class _NestedProbeIncomplete(Exception):
    """A skipping nested walk read a repo's config but could not finish probing it."""


_NESTED_REPO_INCOMPLETE = "repository could not be fully checked for exec drivers"


def _repo_scoped_config_keys(
    proj: str,
    *,
    _depth: int = 0,
    _walked: list[str] | None = None,
    _skip_unreadable: bool = False,
) -> list[str] | None:
    """Every key name the repository's own config scopes declare, ``None`` if unreadable.

    The ``--local`` scope always, and ``--worktree`` when
    ``extensions.worktreeConfig`` is on; both with ``--includes``. The details
    are documented on :func:`repo_exec_config_reason`, which this serves.

    Also every INITIALIZED nested repository the index records as a gitlink,
    recursively: ``status``, a worktree ``diff`` and ``reset`` start a child git
    in each one, and that child reads the nested repository's OWN config, which
    sits inside the work tree an agent writes. The ``GIT_CONFIG_*`` pins reach
    the child; a repo-named driver declared there does not, and was measured to
    run (git 2.47). An uninitialized gitlink has no work tree, so nothing
    recurses into it.
    """
    keys = _own_scoped_config_keys(proj)
    if keys is None:
        return None
    # A failure below would throw away keys already read; the nested-only walk
    # refuses instead.
    keys = _repo_scoped_config_keys_after_own(
        proj, keys, _depth=_depth, _walked=_walked, _skip_unreadable=_skip_unreadable
    )
    if keys is None and _skip_unreadable:
        raise _NestedProbeIncomplete
    return keys


def _repo_scoped_config_keys_after_own(
    proj: str,
    keys: list[str],
    *,
    _depth: int,
    _walked: list[str] | None,
    _skip_unreadable: bool,
) -> list[str] | None:
    """The rest of :func:`_repo_scoped_config_keys` once *proj*'s own *keys* are read."""
    # The scope listings do not follow an `includeIf "hasconfig:remote.*.url:..."`,
    # which can match on a URL from GLOBAL config. Git's own effective view,
    # scope-tagged and read under the protected command's env (`_PROBE_ENV`), sees
    # what that command will load; only the repository's own scopes are kept.
    effective = _git_probe(
        proj, "config", "--includes", "--show-scope", "--name-only", "-z", "--list"
    )
    if effective is None:
        return None
    # `-z --show-scope --name-only` prints `<scope>\0<key>\0` per entry.
    fields = effective.split("\0")
    user_drivers: list[str] = []
    for scope, key in zip(fields[0::2], fields[1::2]):
        if not key:
            continue
        if scope in ("local", "worktree"):
            keys.append(key.strip())
        elif _PROBE_ALL_SCOPES.get() and _USER_DRIVER_KEY_RE.match(key.strip()):
            user_drivers.append(key.strip())
    if user_drivers:
        # A driver from the operator's global/system config (`git lfs install`
        # writes one) counts only when THIS repo's attributes bind its name: that
        # is what makes the child git run it here.
        bound = _bound_driver_names(proj)
        if bound is None:
            return None
        # A driver literally named `unset`/`unspecified` cannot be told apart from
        # an absent attribute in `check-attr` output, so it always counts.
        keys.extend(
            k
            for k in user_drivers
            if _driver_attr_and_name(k) in bound
            or _driver_attr_and_name(k)[1] in ("unset", "unspecified")
        )
    nested = _nested_repo_config_keys(
        proj, _depth=_depth, _walked=_walked, _skip_unreadable=_skip_unreadable
    )
    if nested is None:
        return None
    return keys + nested


def _nested_repo_config_keys(
    proj: str,
    *,
    _depth: int = 0,
    _walked: list[str] | None = None,
    _skip_unreadable: bool = False,
) -> list[str] | None:
    """The config key names of every initialized nested repo under *proj*, recursively.

    ``None`` when *proj*'s own index cannot be listed. With *_skip_unreadable* (the
    nested-only probe), a nested repo that cannot be fully probed raises
    :class:`_NestedProbeIncomplete` instead, so only *proj*'s own unreadable index
    reads as ``None``.
    """
    walked = _walked if _walked is not None else []
    keys: list[str] = []
    # Joined onto git's EFFECTIVE work tree, not *proj*: a `core.worktree`
    # redirect puts the nested repos git will descend into somewhere else, and a
    # read-only caller does not refuse the redirect itself. `--full-name` makes
    # the listed paths relative to that root. utf8_path_stdout: the names are
    # joined onto a path and lstat'ed, so raw bytes must survive.
    toplevel = _git_probe(proj, "rev-parse", "--show-toplevel", decoder=utf8_path_stdout)
    if toplevel is None or not toplevel.rstrip("\n"):
        return None
    root = toplevel[:-1] if toplevel.endswith("\n") else toplevel
    listing = _git_probe(proj, "ls-files", "-s", "-z", "--full-name", decoder=utf8_path_stdout)
    if listing is None:
        return None
    for entry in listing.split("\0"):
        meta, _, rel = entry.partition("\t")
        if not meta.startswith("160000 ") or not rel:
            continue
        nested = os.path.join(root, rel)
        if not os.path.lexists(os.path.join(nested, ".git")):
            continue
        walked.append(nested)
        if _depth + 1 > _MAX_NESTED_REPO_DEPTH or len(walked) > _MAX_NESTED_REPOS:
            raise _NestedRepoLimit
        inner = _repo_scoped_config_keys(
            nested, _depth=_depth + 1, _walked=walked, _skip_unreadable=_skip_unreadable
        )
        if inner is None:
            # Not skipped even when skipping: a nested repo with its config
            # deleted still gets a child git that runs a GLOBAL driver its
            # attributes bind, so "cannot list it" is never "nothing runs there".
            if _skip_unreadable:
                raise _NestedProbeIncomplete
            return None
        keys.extend(inner)
    return keys


def _own_scoped_config_keys(proj: str) -> list[str] | None:
    """The ``--local``/``--worktree`` key names of *proj* alone (no nested repos)."""
    scopes = ["--local"]
    # --bool folds every git-true spelling (yes/on/1/valueless) to "true"; a
    # raw-spelling allowlist misses the valueless boolean form, which git
    # still honors for config.worktree. A garbled value makes --bool exit
    # non-zero (reads as "") AND kills the guarded git command itself with
    # the same parse error, so skipping the scope is safe.
    if (
        _git(
            proj,
            "config",
            "--local",
            "--includes",
            "--bool",
            "--get",
            "extensions.worktreeConfig",
        )
        == "true"
    ):
        scopes.append("--worktree")
    keys: list[str] = []
    for scope in scopes:
        listing = _git_probe(proj, "config", scope, "--includes", "--name-only", "-z", "--list")
        if listing is None:
            # Probe-first, classify after: git creates config.worktree lazily,
            # so a --worktree listing that failed on a genuinely ABSENT file is
            # the empty scope git documents, not an unreadable one (the shared
            # decision in kiro_crew.git_worktree_scope). Every other failure
            # stays _EXEC_CONFIG_UNREADABLE, including an unlocatable git dir.
            # RAW stdout (_git_probe, not _git): the classifier trims exactly
            # git's newline, and a stripped whitespace-bearing path would
            # lstat the wrong location.
            if scope == "--worktree":
                # utf8_path_stdout: this answer is handed to the classifier's
                # ``os.lstat``, so a non-UTF-8 byte in the real path must
                # survive as a PEP 383 surrogate ``os.fsencode`` restores
                # byte-exactly -- a U+FFFD from the display decode would miss
                # an existing ``config.worktree`` and clear a scope git reads.
                gitdir_raw = _git_probe(
                    proj,
                    "rev-parse",
                    "--absolute-git-dir",
                    decoder=utf8_path_stdout,
                )
                if worktree_probe_failure_is_empty_scope(
                    gitdir_raw if gitdir_raw is not None else "", proj
                ):
                    continue
            return None
        keys.extend(key.strip() for key in listing.split("\0") if key.strip())
    return keys


def hidden_worktree_edits(proj: str) -> list[str] | None:
    """Paths whose changes ``git status`` CANNOT see, and which differ from HEAD.

    ``git update-index --assume-unchanged`` / ``--skip-worktree`` tell git to stop
    checking a path, and git obeys them thoroughly: for an edited
    assume-unchanged file both ``git status --porcelain`` and ``git diff --quiet
    HEAD`` report a CLEAN tree, while ``reset --hard`` still overwrites the file.
    That makes it the one tracked-file loss the work-tree refusal cannot see --
    verified end to end, edit destroyed.

    Read-only by construction. The documented remedy, ``update-index
    --really-refresh``, does surface the edit, but it WRITES the index: a check
    whose only purpose is to decide whether mutating the checkout is safe must not
    mutate it to find out, and doing so unattended would also silently disturb the
    developer's own index state.

    Instead the flagged entries are enumerated (``ls-files -v``, where a LOWERCASE
    tag means "not checked") and each one is hashed and compared with the blob HEAD
    records.

    The hash lets git apply the path's attributes, and deliberately does NOT pass
    ``--no-filters``. That flag suppresses git's BUILT-IN conversions as well as
    external ones, so under ``core.autocrlf`` -- the normal Windows posture -- an
    UNMODIFIED file hashes differently from its blob (working CRLF vs stored LF)
    and this check would refuse every such checkout. Verified: with
    ``--no-filters`` an untouched file mismatches, with attributes applied it
    matches and a real edit is still detected. Letting attributes apply is safe
    here precisely because a repository declaring a filter driver is refused
    before this runs (:func:`repo_exec_config_reason`), so no external program can
    be reached through it.

    Returning only paths that ACTUALLY differ matters: flagging every
    assume-unchanged entry would refuse the update for anyone who uses the bit for
    local config overrides, which is the silent no-op this whole change exists to
    remove.

    ``None`` means git could not answer, which the caller must treat as unsafe.
    """
    listing = _git_probe(proj, "ls-files", "-v")
    if listing is None:
        return None

    flagged: list[str] = []
    for line in listing.splitlines():
        # "<tag> <path>"; lowercase tag == assume-unchanged, "S" == skip-worktree.
        if len(line) < 3 or line[1] != " ":
            continue
        tag, path = line[0], line[2:]
        if tag.islower() or tag == "S":
            flagged.append(path)
    if not flagged:
        return []

    differing: list[str] = []
    for path in flagged:
        recorded = _git_probe(proj, "rev-parse", f"HEAD:{path}")
        actual = _git_probe(proj, "hash-object", "--", path)
        if recorded is None or actual is None:
            # Cannot compare this one, so cannot prove it is safe.
            return None
        if recorded.strip() != actual.strip():
            differing.append(path)
    return differing


def loggable_path(name: str) -> str:
    """*name* rendered so it is always UTF-8 encodable, for a log record.

    Filesystem names reach this module through ``os.fsdecode``, which is what the
    ``os.path`` calls need but which represents a byte that is not valid UTF-8 as
    a LONE SURROGATE (``b"\\xff"`` becomes ``"\\udcff"``). A surrogate cannot be
    encoded to UTF-8, so interpolating one into a log record raises
    ``UnicodeEncodeError`` inside the handler. ``logging`` does not propagate that
    -- it calls ``handleError`` and DROPS the record -- so the line that would be
    lost is the one saying an unattended update refused in order to preserve the
    user's file. The same string also breaks any UTF-8 log shipper, and
    ``pytest-xdist``, which serializes reports as UTF-8 (a captured record with a
    surrogate crashed an entire CI shard with ``DumpError``).

    Round-trips to the ORIGINAL bytes and escapes only the un-encodable ones, so
    the operator sees the real on-disk byte (``bad\\xffname.txt``) rather than a
    replacement character. ASCII and valid non-ASCII names (CJK, accents) pass
    through unchanged, so this costs nothing in readability for the normal case.

    Use it for the LOG only. The un-decorated ``os.fsdecode`` form is the one that
    must be passed to the filesystem.
    """
    return os.fsencode(name).decode("utf-8", "backslashreplace")


def commits_ahead(proj: str, upstream: str) -> int | None:
    """How many commits ``HEAD`` is ahead of *upstream*, or ``None`` if unknown.

    ``None`` means git could not answer, which the caller must treat exactly as
    it treats a positive count: an unattended ``reset --hard`` may not run when
    we cannot prove there is nothing to lose.

    *upstream* is taken as given — pass the SAME revision the reset will use,
    which for the update path is the captured OID rather than a ref name. That is
    not a detail: a ref is re-resolved per command, so counting against
    ``origin/<branch>`` while resetting to an already-captured OID lets a
    concurrent fetch advance the ref, report zero commits ahead of the NEW tip,
    and still reset to the OLD one — discarding exactly the commits the count was
    supposed to protect. Counting against the reset target closes that.

    This exists because the two checks that look like they cover it do not.
    ``git status --porcelain`` reports working-tree edits, not commits, and the
    availability probe (``git diff HEAD <target> --quiet``) is satisfied by a
    difference in EITHER direction — so a checkout carrying local commits passes
    both and then loses them to the reset.
    """
    if not upstream:
        return None
    out = _git_probe(proj, "rev-list", "--count", f"{upstream}..HEAD")
    if out is None:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


def tracks_upstream(proj: str, branch: str, *, remote: str = "origin") -> bool:
    """Whether *branch* in *proj* tracks exactly ``<remote>/<branch>``.

    The unattended update FETCHES and RESETS to ``<remote>/<branch>``, and the
    source pin validates that same fixed remote. But the availability check that
    decides an update exists compares ``HEAD`` against ``@{u}`` — whatever the
    branch actually tracks. When those two are not the same ref, the check
    measures one thing and the reset applies another, and the reset is a
    ``--hard`` one, so the gap is lost commits rather than a stale answer.

    BOTH halves of the upstream are therefore checked, because either alone
    leaves the gap open:

    * the remote — a fork checkout whose ``main`` tracks ``upstream/main`` while
      ``origin`` is the user's own stale fork;
    * the branch — ``branch.main.remote=origin`` with
      ``branch.main.merge=refs/heads/other``, which still points ``@{u}`` at
      ``origin/other`` while the reset targets ``origin/main``.

    A checkout that tracks anything else still gets the badge, and can update
    through ``kirocrew update`` or the dashboard, both of which have a human
    deciding.
    """
    if not branch or branch == "HEAD":
        return False
    if _git(proj, "config", "--get", f"branch.{branch}.remote") != remote:
        return False
    return _git(proj, "config", "--get", f"branch.{branch}.merge") == f"refs/heads/{branch}"


def update_blocked_reason(remote_url: str) -> str:
    """Why this update is not allowed, or ``""`` when it is.

    The one gate the three update paths call. The returned string is
    operator-facing (it reaches an API 403 body and the CLI's stderr), so it names
    neither the remote nor the pin: a git remote can embed a token
    (``https://x-access-token:<pat>@host/…``, ``?access_token=…``) and so can the
    pin. The operator can read both from `git remote -v` and the policy file; the
    log/response needs only to say which check failed.
    """
    from kiro_crew.platform.governance import active_update_pins

    if not active_update_pins().permits_source(remote_url):
        return (
            "this checkout's git remote does not match the update source pinned "
            "by the security policy"
        )
    return ""


def update_required(current_version: str) -> bool:
    """Is this build below the fleet's pinned minimum version?

    ``True`` makes an update MANDATORY — it overrides the user's
    ``auto_update=False``, because user config sits under the enterprise ceiling
    and an operator opting out must not be able to hold a fleet on a build the
    policy forbids. It never refuses to boot: bricking a fleet on a policy typo
    would remove the very surface an admin needs to fix it.
    """
    from kiro_crew.platform.governance import active_update_pins

    return not active_update_pins().meets_min_version(current_version)


def min_version() -> str:
    """The pinned minimum version, or ``""`` when unpinned (for display)."""
    from kiro_crew.platform.governance import active_update_pins

    return active_update_pins().min_version


def unattended_apply_allowed() -> bool:
    """Whether policy lets the gateway install an update without being asked.

    ``updates.unattended_apply: false`` makes every route notify-only; a
    person's Update still applies, and a build below the floor still takes its
    mandatory update. True when ungoverned.
    """
    from kiro_crew.platform.governance import active_update_pins

    return active_update_pins().unattended_apply


__all__ = [
    "PRIMARY_BRANCHES",
    "git_command_env",
    "git_neutralizer_env",
    "is_primary_branch",
    "commits_ahead",
    "min_version",
    "repo_exec_config_reason",
    "nested_repo_exec_driver_reason",
    "repo_exec_driver_reason",
    "resolve_remote_url",
    "tracks_upstream",
    "unattended_apply_allowed",
    "update_blocked_reason",
    "update_required",
    "upstream_remote_and_branch",
]
