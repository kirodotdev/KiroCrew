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

import logging
import os
import re
import subprocess

from kiro_crew import platform_compat
from kiro_crew.subprocess_utf8 import UTF8_TEXT

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_SECS = 10


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


def _git(proj: str, *args: str) -> str:
    """Run a read-only git command in *proj*, returning stripped stdout or ``""``."""
    out = _git_probe(proj, *args)
    return out.strip() if out is not None else ""


def _git_probe(proj: str, *args: str) -> str | None:
    """As :func:`_git`, but ``None`` when git could not answer at all.

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
    git = platform_compat.trusted_git_bin()
    if git is None:
        # No trustworthy git. Answer "could not determine", which every caller
        # already treats as the unsafe direction — never fall back to a bare
        # `"git"`, which is the hazard itself.
        return None
    try:
        done = subprocess.run(
            [git, *args],
            cwd=proj,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECS,
            env=git_command_env(),
            **UTF8_TEXT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def resolve_remote_url(proj: str, *, remote: str = "", branch: str = "") -> str:
    """The URL of the remote an update would fetch from in *proj* (``""`` if unknown).

    Pass *remote* for a FIXED remote name: the CLI and gateway paths run
    ``git fetch origin <branch>``, so they must validate ``origin`` rather than
    whatever the branch tracks. With no *remote*, ``branch.<name>.remote`` is
    resolved instead — that is the API path's bare ``git pull``, which follows the
    tracked remote. Validating a different remote than the one fetched would
    approve one source and install another.

    ``ls-remote --get-url`` (not ``remote get-url``) additionally applies
    ``url.<base>.insteadOf`` rewriting, so the value checked is the URL git
    resolves rather than the one merely written down.

    Returns ``""`` on any failure, which a source pin then treats as "not
    permitted" and an unpinned host ignores.
    """

    if not remote:
        if not branch:
            branch = _git(proj, "rev-parse", "--abbrev-ref", "HEAD")
        if not branch or branch == "HEAD":  # detached HEAD tracks nothing
            return ""
        remote = _git(proj, "config", "--get", f"branch.{branch}.remote") or "origin"
    url = _git(proj, "ls-remote", "--get-url", "--", remote)
    # `--get-url` echoes its argument back for an unknown remote; that is a bare
    # name, not a URL, and must not be checked as one.
    return "" if url == remote else url


#: Repo-scoped config keys whose VALUE git may execute as a program, and whose
#: KEY NAME is fixed so ``-c``/``GIT_CONFIG_*`` can pin it back to a safe value.
#:
#: The membership criterion is exactly that — "git may exec this value, and the
#: key is a literal" — and it is written down because the first version of this
#: list was an enumeration without one and was therefore missing members
#: (``core.gitProxy``). Adding a key here is correct whenever git might exec it;
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
    # Signature VERIFICATION is the trigger here: a fetched commit carrying a
    # gpgsig header would invoke gpg.program. The update never verifies
    # signatures, so the only reason git would exec it is an attacker's.
    ("merge.verifySignatures", "false"),
    ("pull.verifySignatures", "false"),
    # Local/file transports exec the config-named pack programs directly, and
    # GIT_ALLOW_PROTOCOL does not gate them (they are not a protocol). Every
    # update fetch uses the literal remote `origin`, so pinning these restores
    # git's own defaults over anything in .git/config.
    ("remote.origin.uploadpack", "git-upload-pack"),
    ("remote.origin.receivepack", "git-receive-pack"),
    # `ext::` remote URLs run an arbitrary command as the transport.
    ("protocol.ext.allow", "never"),
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
_PROGRAM_VALUED_PINS = frozenset(
    {
        "core.sshcommand",
        "core.pager",
        "core.editor",
        "sequence.editor",
        "gpg.program",
        "remote.origin.uploadpack",
        "remote.origin.receivepack",
    }
)


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
    r"^url\..+\.(?:insteadof|pushinsteadof)$",
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
_REPO_EXEC_DRIVER_RE = re.compile(
    r"^(?:filter\.(?P<f>.+)\.(?:process|smudge|clean)"
    # `textconv` converts a blob to text; `command` REPLACES the whole diff with
    # an external program. Both are repo-named and both are reached by the
    # `git diff` this path runs, so refusing only one left the other open.
    r"|diff\.(?P<d>.+)\.(?:textconv|command)"
    # `credential.<url>.helper` is per-URL and so repo-named: pinning the bare
    # `credential.helper` does not reach it.
    r"|credential\.(?P<c>.+)\.helper"
    # `remote.<name>.vcs` names a TRANSPORT helper: git execs `git-remote-<value>`,
    # resolved through PATH, so the fetch itself runs a repo-chosen program.
    # Verified, and verified to be unpinnable in the useful sense: an empty pin
    # does suppress the repo's helper, but git then treats "" as a helper name and
    # every fetch dies with `remote helper '' aborted session` -- so pinning would
    # disable the update path instead of protecting it.
    r"|remote\.(?P<r>.+)\.vcs)$",
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


def git_command_env() -> dict[str, str]:
    """A complete environment for running git against a discovered checkout.

    The one env a caller should use. It is built rather than merged because the
    hazard here is a variable that must be ABSENT, and merging a neutralizer dict
    over ``os.environ`` can only add or overwrite keys — an inherited
    ``GIT_DIR`` would survive that and silently retarget the command.

    Combines the exec-vector pins from :func:`git_neutralizer_env` with the
    removal of every :data:`_GIT_LOCATION_VARS` entry.
    """
    env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_VARS}
    env.update(git_neutralizer_env())
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


def git_neutralizer_env() -> dict[str, str]:
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
    pins = _GIT_EXEC_NEUTRALIZERS + _GIT_BLAST_RADIUS_PINS
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


def _same_dir(a: str, b: str) -> bool:
    """Whether two paths name the same directory, symlinks and case resolved.

    ``os.path.realpath`` on both sides so a symlinked checkout (this repo is
    reached through one) does not read as a redirect, and ``normcase`` so a
    Windows drive-letter or case difference does not either.
    """
    if not a or not b:
        return False
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def repo_exec_config_reason(proj: str) -> str:
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

    Global and system config are deliberately not probed: that is the user's own
    machine configuration, not something the repository supplies.
    """
    scopes = ["--local"]
    if _git(
        proj, "config", "--local", "--includes", "--get", "extensions.worktreeConfig"
    ).lower() in (
        "true",
        "yes",
        "on",
        "1",
    ):
        scopes.append("--worktree")
    for scope in scopes:
        listing = _git_probe(proj, "config", scope, "--includes", "--name-only", "--list")
        if listing is None:
            return _EXEC_CONFIG_UNREADABLE
        for line in listing.splitlines():
            key = line.strip()
            if _REPO_EXEC_DRIVER_RE.match(key):
                return f"repository declares {key[:120]}"
            if key.lower() in _REPO_UNPINNABLE_KEYS:
                return f"repository declares {key[:120]}"
            # A URL rewrite redirects the FETCH itself, and applies even when the
            # URL is passed explicitly on the command line, so validating the
            # remote's URL cannot survive one being configured.
            if _REPO_URL_REWRITE_RE.match(key):
                return f"repository rewrites remote URLs via {key[:120]}"
            # Transport trust: weakening TLS verification or routing the fetch
            # through a repo-supplied proxy makes a forged update look genuine.
            if _REPO_TRANSPORT_TRUST_RE.match(key):
                return f"repository overrides transport trust via {key[:120]}"

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
    ``origin/<branch>`` while resetting to a previously captured OID lets a
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


__all__ = [
    "PRIMARY_BRANCHES",
    "git_command_env",
    "git_neutralizer_env",
    "is_primary_branch",
    "commits_ahead",
    "min_version",
    "repo_exec_config_reason",
    "resolve_remote_url",
    "tracks_upstream",
    "update_blocked_reason",
    "update_required",
]
