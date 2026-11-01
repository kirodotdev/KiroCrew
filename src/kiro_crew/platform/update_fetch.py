"""The isolated fetch the update check and the unattended apply share.

Split out of :mod:`kiro_crew.platform.update_governance`, which owns the guards
(the exec-vector pins, the config refusals and the source pin). This module owns
the one git operation of the update seam that WRITES a ref: the fetch, run
against a throwaway git dir so the checkout's own config can name no hook that
fires on it. Every git read it makes goes through that module's guarded probe.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from collections.abc import Mapping

from kiro_crew import platform_compat
from kiro_crew.platform.update_governance import (
    _OID_RE,
    _git,
    _git_probe,
    _local_fetch_url,
    git_command_env,
    loggable_path,
    resolve_remote_url,
)

logger = logging.getLogger(__name__)


#: A git config key under ``http`` that carries an auth header, bare or
#: URL-scoped (``http.extraHeader``, ``http.https://host/.extraHeader``). The
#: URL-scoped form is what ``actions/checkout`` writes, so an exact-key lookup
#: for the bare name alone breaks an authenticating checkout's update fetch.
_HTTP_EXTRA_HEADER_RE = re.compile(r"^http\.(?:.+\.)?extraheader$", re.IGNORECASE)


def _scratch_http_auth_pairs(proj: str) -> list[tuple[str, str]]:
    """``(key, value)`` pairs re-applying the checkout's ``http.*.extraHeader`` auth.

    A checkout that authenticates only through a repo-local ``http.extraHeader``
    or ``http.<url>.extraHeader`` (the form ``actions/checkout`` writes) must
    still fetch through the isolated git dir, whose own config carries none of
    it. Every such key is forwarded, in config order. The caller applies them
    through ``GIT_CONFIG_COUNT``/``GIT_CONFIG_KEY_n``/``GIT_CONFIG_VALUE_n``,
    which git takes verbatim and NEVER re-splits -- unlike a ``-c key=value``
    argument, which git splits at the first ``=`` so a subsection URL containing
    ``=`` could smuggle a ``proxy``/``sslcainfo`` setting past the key filter and
    redirect the transport. A value with a newline or NUL is dropped. A header is
    DATA, never a program, so forwarding it is not an exec vector the isolation
    exists to close, and the transport-trust keys (``http.sslVerify`` and the
    CA/cert/proxy family) are refused upstream by :func:`repo_exec_config_reason`
    before any fetch, so none reach here.
    """
    listing = _git_probe(proj, "config", "--get-regexp", r"^http\.")
    if not listing:
        return []
    pairs: list[tuple[str, str]] = []
    for line in listing.splitlines():
        key, _, value = line.partition(" ")
        if _HTTP_EXTRA_HEADER_RE.match(key) and "\n" not in value and "\0" not in value:
            pairs.append((key, value))
    return pairs


def _prepare_isolated_fetch(
    proj: str, remote: str, branch: str
) -> tuple[str, str, str, list[tuple[str, str]]] | None:
    """Resolve the URL and build the throwaway git dir, off the event loop.

    Returns ``(url, objects_dir, scratch, http_auth_pairs)`` or ``None`` when the
    remote has no usable URL or the object store cannot be located.

    The object store is asked of git (``rev-parse --git-path objects``), not
    hard-coded to ``proj/.git``, so a linked worktree or a submodule -- whose
    ``.git`` is a pointer file -- resolves to its real common object store. The
    scratch dir is created under the sandbox-masked crew scratch root, NOT the
    shared system tempdir: a same-UID sandboxed agent can write ``/tmp`` and
    would race a hook into the scratch config, but the masked root it cannot
    reach. The scratch repo inherits the checkout's OBJECT FORMAT (a SHA-256
    checkout cannot be fetched through a SHA-1 scratch), but none of its
    executable config. The filesystem work and git probes are grouped here so
    the async caller runs them in one thread rather than touching disk on the
    loop.
    """
    from kiro_crew import agent_scratch

    url = resolve_remote_url(proj, fetch_env=git_command_env(), remote=remote)
    if not url:
        logger.warning("Update fetch: no usable URL for remote %s", remote)
        return None
    url = _local_fetch_url(proj, url)
    objects_dir = _git(proj, "rev-parse", "--path-format=absolute", "--git-path", "objects")
    if not objects_dir or not os.path.isdir(objects_dir):
        logger.warning("Update fetch: %s has no object store to fetch into", loggable_path(proj))
        return None
    object_format = _git(proj, "rev-parse", "--show-object-format") or "sha1"

    root = agent_scratch.scratch_root()
    os.makedirs(root, exist_ok=True)
    scratch = tempfile.mkdtemp(prefix="kirocrew-update-fetch-", dir=str(root))
    for sub in ("objects/info", "objects/pack", "refs/heads", "refs/tags", "refs/kirocrew-have"):
        os.makedirs(os.path.join(scratch, sub), exist_ok=True)
    with open(os.path.join(scratch, "HEAD"), "w", encoding="utf-8") as handle:
        handle.write("ref: refs/heads/_kirocrew_update\n")
    # SHA-256 needs repositoryformatversion=1 + the extensions entry; SHA-1 is
    # the version-0 default. Only this non-executable setting is carried over.
    if object_format == "sha256":
        config = "[core]\n\trepositoryformatversion = 1\n\tbare = true\n[extensions]\n\tobjectformat = sha256\n"
    else:
        config = "[core]\n\trepositoryformatversion = 0\n\tbare = true\n"
    with open(os.path.join(scratch, "config"), "w", encoding="utf-8") as handle:
        handle.write(config)
    # A shallow checkout's boundary commits live in its `shallow` file. The
    # scratch repo shares the object store but not that file, so without it the
    # negotiation walks past the boundary into missing parents and fails. Copied
    # line by line, each one checked to be an object id.
    shallow_src = _git(proj, "rev-parse", "--path-format=absolute", "--git-path", "shallow")
    if shallow_src and os.path.isfile(shallow_src):
        with open(shallow_src, encoding="utf-8", errors="replace") as handle:
            boundary = [line.strip() for line in handle if line.strip()]
        if not all(_OID_RE.match(oid) for oid in boundary):
            logger.warning("Update fetch: %s has an unreadable shallow file", loggable_path(proj))
            shutil.rmtree(scratch, ignore_errors=True)
            return None
        with open(os.path.join(scratch, "shallow"), "w", encoding="utf-8") as handle:
            handle.write("".join(oid + "\n" for oid in boundary))
    # Seed the commits the checkout already has as scratch refs, so the fetch
    # negotiation offers them as "have"s and the server sends only the delta.
    # Without any ref the scratch repo would re-download the whole history into
    # a fresh pack on every poll. The existing remote-tracking tip and HEAD both
    # live in the common object store the fetch writes to, so they are valid
    # haves. Written as loose refs in the scratch dir this owns (no proj write).
    haves = [
        _git(proj, "rev-parse", "--verify", "--quiet", "HEAD"),
        _git(proj, "rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{branch}"),
    ]
    for index, have in enumerate(haves):
        if _OID_RE.match(have):
            with open(os.path.join(scratch, "refs", "kirocrew-have", str(index)), "w") as handle:
                handle.write(have + "\n")
    return url, objects_dir, scratch, _scratch_http_auth_pairs(proj)


def _read_fetched_commit(proj: str, scratch: str) -> str | None:
    """The fetched COMMIT id, read from *scratch*'s ``FETCH_HEAD`` and verified.

    The raw ``FETCH_HEAD`` id is peeled and checked to be a commit (``rev-parse
    --verify <oid>^{commit}``) against *proj*'s object store, where the fetch
    deposited it. Fetching ``refs/heads/<branch>`` already keeps a same-named
    upstream TAG out of the answer; this is the belt-and-suspenders that a
    non-commit id is never handed to ``reset --hard``. ``None`` when
    ``FETCH_HEAD`` is unreadable or the id is not a commit.
    """
    try:
        with open(os.path.join(scratch, "FETCH_HEAD"), encoding="utf-8", errors="replace") as fh:
            first = fh.readline().split()
    except OSError:
        return None
    oid = first[0].strip() if first else ""
    if not _OID_RE.match(oid):
        return None
    commit = _git(proj, "rev-parse", "--verify", "--quiet", f"{oid}^{{commit}}")
    return commit if _OID_RE.match(commit) else None


async def fetch_and_pin_upstream(
    proj: str,
    remote: str,
    branch: str,
    *,
    git: str,
    env: Mapping[str, str],
    timeout: float = 30.0,
) -> str | None:
    """Fetch ``<remote>/<branch>`` for *proj* and return the fetched commit id.

    The one git operation in the update seam that WRITES a ref -- the fetch --
    is the one the checkout's own config can hook: the ref update fires
    ``reference-transaction``, and a ``hook.<name>.command`` raced into
    ``.git/config`` after the exec-config preflight read it runs on the host.
    No ``-c`` pin or fetch flag closes that (verified: even a ``FETCH_HEAD``-only
    fetch opens a ref transaction).

    So the fetch runs against a THROWAWAY git dir this builds and owns, whose
    config carries only our pins and names no hook, while its objects are
    deposited straight into *proj*'s own object store (``GIT_OBJECT_DIRECTORY``)
    so the caller reads and resets against *proj*. The fetched tip is returned as
    an OID the caller uses directly; no ref in *proj* is written, so there is no
    ref transaction and no hook. (*proj*'s remote-tracking ref is left as the
    last ``git fetch`` / ``kirocrew update`` set it -- ``kirocrew doctor`` already
    says "as of last fetch". Refreshing it here from agent-controlled
    ``remote``/``branch`` was tried and drew repeated path-traversal findings for
    a cosmetic gain, so it is deliberately not done.) Returns the fetched COMMIT
    id, or ``None`` on any failure (a refused source URL, a fetch error or
    timeout, a tip that is not a commit).

    ``refs/heads/<branch>`` is fetched explicitly so a same-named upstream TAG
    cannot stand in for the branch tip, and the id is verified to be a commit.
    The fetch runs with ``cwd=proj`` so a relative local remote resolves against
    the checkout, while ``GIT_DIR`` keeps the fetch's own ref write in the
    scratch dir. A repo-local ``http.*.extraHeader`` is forwarded through
    ``GIT_CONFIG_*`` env (never a re-splittable ``-c``) so an authenticating
    checkout still fetches.

    The scratch dir is assembled in Python with no ``git`` subprocess, so the
    only child this spawns is the network fetch itself -- the process a timeout
    or a shutdown cancellation must kill and reap.
    """
    prepared = await asyncio.to_thread(_prepare_isolated_fetch, proj, remote, branch)
    if prepared is None:
        return None
    url, objects_dir, scratch, http_auth_pairs = prepared
    try:
        fetch_env = dict(env)
        fetch_env["GIT_DIR"] = scratch
        fetch_env["GIT_OBJECT_DIRECTORY"] = objects_dir
        # PATH is pinned for this fetch only (callers pass a trusted absolute
        # git). git resolves a transport helper and `ssh` off PATH at exec time,
        # and a gateway PATH can lead with an agent-writable directory. Not in
        # git_command_env: callers there may spawn a bare `git` found on PATH.
        trusted_path = platform_compat.trusted_system_path()
        if trusted_path is not None:
            fetch_env["PATH"] = trusted_path
        # Forward the checkout's http auth headers at `-c` precedence via the
        # GIT_CONFIG_* env (git never re-splits these), appended after the pins
        # git_command_env already set.
        count = int(fetch_env.get("GIT_CONFIG_COUNT", "0"))
        for key, value in http_auth_pairs:
            fetch_env[f"GIT_CONFIG_KEY_{count}"] = key
            fetch_env[f"GIT_CONFIG_VALUE_{count}"] = value
            count += 1
        fetch_env["GIT_CONFIG_COUNT"] = str(count)

        proc = await asyncio.create_subprocess_exec(
            git,
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-recurse-submodules",
            "--no-auto-gc",
            "--upload-pack=git-upload-pack",
            "--",
            url,
            f"refs/heads/{branch}",
            cwd=proj,
            env=fetch_env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
        )
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.CancelledError:
            await platform_compat.kill_and_reap(proc)
            raise
        except asyncio.TimeoutError:
            await platform_compat.kill_and_reap(proc)
            logger.warning("Update fetch timed out")
            return None
        if proc.returncode != 0:
            logger.warning(
                "Update fetch failed (rc=%s): %s",
                proc.returncode,
                (err or b"").decode(errors="replace").strip(),
            )
            return None

        commit = await asyncio.to_thread(_read_fetched_commit, proj, scratch)
        if commit is None:
            logger.warning("Update fetch: FETCH_HEAD is not a commit")
            return None
        return commit
    finally:
        await asyncio.to_thread(shutil.rmtree, scratch, ignore_errors=True)


__all__ = ["fetch_and_pin_upstream"]
