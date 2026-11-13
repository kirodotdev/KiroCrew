"""An app's ``app.json`` for the store: where it is read from, fetched and merged.

Entry subdirectories are gated lexically and joined with a symlink-resolving
containment check. The manifest comes from the verified persistent checkout or a
throwaway clone, under the credential posture the entry earns. The merge projects
index rows through ``_REGISTRY_ROW_KEYS`` and rewrites store art through the blob
proxy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.apps import official_catalog
from kiro_crew.apps.registry_pipeline import _FACADE
from kiro_crew.apps.registry_pipeline.caches import (
    _MANIFEST_NEGATIVE_CACHE_TTL,
    _read_installed_manifest_cache,
    _read_manifest_cache,
    _read_manifest_negative_cache,
    _write_installed_manifest_cache,
    _write_manifest_cache,
    _write_manifest_negative_cache,
)
from kiro_crew.apps.registry_pipeline.checkout import (
    _CLONE_TIMEOUT,
    _HOOKS_NEUTRALIZER_ARGV,
    _clone_branch_matches,
    _clone_origin_matches,
    _communicate_with_timeout,
    _git_fetch_branch,
    _git_fetch_commit,
    _mkdtemp_settled,
    _rmtree_force_settled,
)
from kiro_crew.apps.registry_pipeline.git_targets import (
    _SSH_REFUSAL_PREFIX,
    _entry_git_url,
    _git_target_is_unsupported,
    _git_transport_env,
    _git_url_host,
    _is_ssh_git_url,
    _loggable_git_transport_output,
    _looks_like_git_url,
    _ssh_method_list_closes,
    _strip_git_target_userinfo,
)
from kiro_crew.apps.registry_pipeline.recovery import app_source_dir
from kiro_crew.apps.registry_pipeline.sources import (
    _context_clone_sandbox_mode,
    _owner_designated_repo_target,
    _sel_credential_grant,
    is_clone_host_trusted,
)
from kiro_crew.apps.registry_pipeline.store_art import _contained_join, _store_asset_path
from kiro_crew.apps.registry_pipeline.subprocess_env import (
    _git_network_env,
    _proxy_env_has_credentials,
    anonymous_git_env,
    minimal_env,
)
from kiro_crew.apps.store_links import sanitize_links
from kiro_crew.sandbox import (
    cgroup_scope_argv,
    create_subprocess_limited,
    wrap_argv,
    wrap_argv_async,
)
from kiro_crew.user_json import loads_user_json

logger = logging.getLogger(_FACADE)


async def _fetch_app_manifest(
    repo: str,
    branch: str,
    subdirectory: str = "",
    app_name: str = "",
    git_url: str = "",
    *,
    owner_designated: bool = False,
    commit: str = "",
    allow_remote: bool = True,
    provenance: dict[str, str] | None = None,
    outcome: dict[str, str] | None = None,
    before_remote: Callable[[], Awaitable[bool]] | None = None,
) -> dict[str, Any] | None:
    """Fetch app.json for an app from its source repo (lightweight).

    Tries, in order:
      1. The persistent clone under ``~/.kiro/crew/app-sources/{app_name}/``
         (if the app was already cloned by a previous install).
      2. A throwaway shallow clone of *git_url* into a temp directory, from
         which only ``app.json`` is read (the clone is then discarded).

    Returns the parsed app.json dict, or None on failure.  All failures are
    swallowed (returns None) so a missing/unreachable repo never crashes the
    listing path on a vanilla machine. *subdirectory* is an untrusted
    index-controlled value; it is joined via :func:`_contained_join` so an
    absolute/``..``/symlink value can never read outside the clone root.

    *owner_designated*: when True (same-repo credential carve-out), the
    clone uses ``minimal_env()`` + context sandbox mode instead of the
    default anonymous+strict posture, and asks to keep the ssh-agent socket
    (``wrap_argv`` re-judges that against the tier). Only set when the entry's
    effective clone URL is byte-identical to the owner-configured registry repo
    URL.

    *commit*: a pinned commit. When set, the manifest is read from THAT tree
    rather than a branch tip, and the local fast path compares commits instead of
    branch names. This matters on the install path specifically: this manifest is
    what the admission gate inspects, so reading it from a branch tip while the
    install fetches a pinned commit would gate one tree and install another --
    and a pinned row carries no branch at all, so the branch would silently be
    the ``"main"`` default.

    *allow_remote*: when False, only step 1 runs -- the local persistent clone
    is still read, and a miss returns None without cloning. The listing passes
    False while a failed-fetch marker is fresh, so an app that is already
    installed keeps its manifest while the remote stays off.

    *provenance*: when given, ``provenance["source"]`` is set to ``"installed"``
    if the answer came from step 1. That copy is the INSTALLED tree, not what the
    branch publishes now, so a caller that caches the answer as the source's
    manifest needs to know (``_resolve_manifest``).

    *outcome*: when given, ``outcome["ssh_sign_in"]`` says what the remote step
    learned: ``"attempted"`` once the SSRF gate passed and a remote fetch began,
    ``"refused"`` when the plain clone failed with ssh's own ``Permission denied
    (<methods>)`` line (``_is_ssh_sign_in_refusal``), and no key when nothing
    remote ran. Over ssh the sign-in
    precedes any repository lookup, so a refusal is about the ssh destination and
    the credential posture, never about this repository. The listing uses it to
    stop asking that destination (``_resolve_manifest``).

    *before_remote*: when given, awaited once the SSRF gate passed, right before
    the remote step. A False answer skips the remote as ``allow_remote=False``
    would, and leaves *outcome* untouched. A row answered from step 1, or refused
    by the gate, never awaits it.
    """

    def _read_manifest_text(path: Path) -> str | None:
        # Blocking: the ``is_file`` probe and the read are both filesystem calls,
        # so each caller below runs this in ONE ``asyncio.to_thread`` hop rather
        # than probing on the event loop. None means "not a regular file"; read
        # errors propagate to the caller's own handler.
        if not path.is_file():
            return None
        return path.read_text("utf-8")

    credential_target = git_url or repo
    if _git_target_is_unsupported(credential_target):
        logger.warning("registry manifest fetch refused an unsupported clone target")
        return None
    git_url = _strip_git_target_userinfo(credential_target)
    credentialed_transport = credential_target != git_url
    # A proxy URL holding a password is a credential too: it takes the split path,
    # so no checkout-time filter inherits it (see `_proxy_env_has_credentials`).
    isolate_transport = credentialed_transport or _proxy_env_has_credentials(git_url)

    # Try persistent clone first (already installed).
    #
    # The persisted clone is keyed on app NAME only, so a registry replacement
    # can leave a checkout of a DIFFERENT repo sitting here under the same
    # name. Its app.json must not stand in for the manifest of the repo we are
    # about to clone: the caller feeds this manifest to the admission gate, and
    # the install that follows discards a stale checkout and re-clones from
    # *git_url* (see _git_clone_or_pull). Trusting the stale copy would admit
    # repo A's manifest and then run repo B's code. So the local copy is only
    # used when the clone's origin still is git_url; otherwise fall through to
    # the throwaway clone of git_url, which always describes what gets cloned.
    if app_name and not commit:
        # A PINNED entry gets no local fast path at all.
        #
        # The persistent checkout is agent-writable, and `app.json` there can be
        # edited without HEAD moving -- so a commit comparison attests where the tree
        # was placed, never what it now holds. This manifest is what the admission and
        # platform gates read, so a local edit bypasses `installMode`/`os`
        # restrictions and gets the tree built server-side. It is the same reason a
        # pinned install never reuses an existing checkout; the rule belongs here too.
        #
        # The cost is a shallow single-commit fetch per pinned listing, which the
        # pinned branch below already performs.
        clone_dir = app_source_dir(app_name)
        manifest_dir = _contained_join(clone_dir, subdirectory)
        local_manifest = manifest_dir / "app.json" if manifest_dir is not None else None
        fresh_enough = await _clone_branch_matches(clone_dir, branch)
        # The file probe and the read share one thread hop, and run before the
        # origin check so a missing manifest still skips that git subprocess.
        local_content: str | None = None
        if local_manifest is not None:
            try:
                local_content = await asyncio.to_thread(_read_manifest_text, local_manifest)
            except (OSError, UnicodeDecodeError):
                local_content = None
        if (
            local_content is not None
            and await _clone_origin_matches(clone_dir, git_url)
            and fresh_enough
        ):
            try:
                local_manifest_data = loads_user_json(local_content)
            except json.JSONDecodeError:
                pass
            else:
                # A local manifest that is not a JSON object is read like an
                # unparseable one: fall through to the fresh clone below.
                if isinstance(local_manifest_data, dict):
                    if provenance is not None:
                        provenance["source"] = "installed"
                    return local_manifest_data

    if not allow_remote:
        return None

    if not _looks_like_git_url(git_url):
        # Not a cloneable URL (e.g. empty or a bare name on a public machine).
        return None
    # SSRF gate: only clone from explicitly-trusted hosts. An untrusted external
    # registry index can list an app repo pointing at an internal address; this
    # listing path clones automatically, so it must not honor such a host.
    # is_clone_host_trusted() loads config from disk (KiroCrewConfig.load), so
    # run it off the event loop to avoid blocking all gateway tasks.
    if not await asyncio.to_thread(is_clone_host_trusted, git_url):
        logger.debug(
            "manifest clone refused for %r: host not in trusted forge/registry set (SSRF gate)",
            _strip_git_target_userinfo(git_url),
        )
        return None
    if before_remote is not None and not await before_remote():
        return None
    if outcome is not None:
        outcome["ssh_sign_in"] = "attempted"

    tmp_root: str | None = None
    try:
        tmp_root = await _mkdtemp_settled("kirocrew-manifest-")
        # Credential posture for the manifest fetch. Default: anonymous+strict
        # (confused-deputy defense — see anonymous_git_env). Same-repo
        # carve-out: when owner_designated is True the clone URL is the
        # owner-configured registry repo itself, so the confused-deputy
        # argument does not apply — use owner credentials + context sandbox.
        # The owner's ssh agent is one of those credentials: the pinned fetch and
        # the plain clone below pass owner_designated as the socket request, and
        # wrap_argv re-judges it against the tier. The credentialed branch is an
        # HTTP fetch and needs no agent.
        if owner_designated:
            clone_env = minimal_env()
            sandbox_mode = _context_clone_sandbox_mode(git_url)
            _sel_credential_grant("fetch_app_manifest", git_url)
        else:
            clone_env = anonymous_git_env()
            sandbox_mode = "strict"

        if commit:
            # Read the manifest from the pinned tree. `--branch` cannot take a
            # commit id, and on the install path this manifest is what the
            # admission gate inspects -- gating a branch tip while installing a
            # pinned commit would check one tree and install another.
            fetch_log: list[str] = []
            # A NONEXISTENT child, not `tmp_root` itself. `tmp_root` already exists
            # (TemporaryDirectory created it), and `_git_fetch_commit` refuses a
            # destination that exists but is not a checkout -- the guard that stops it
            # from adopting, and later deleting, a directory it did not create. Handing
            # it `tmp_root` made every pinned manifest fetch fail, which left the
            # admission and platform-compatibility gates with no manifest at all.
            fetch_dest = Path(tmp_root) / "pinned"
            checkout_root = fetch_dest
            err = await _git_fetch_commit(
                git_url,
                commit,
                fetch_dest,
                fetch_log,
                credential_target=credential_target,
                clone_env=clone_env,
                sandbox_mode=sandbox_mode,
                forward_ssh_auth_sock=owner_designated,
            )
            if err is not None:
                logger.debug(
                    "manifest fetch of pinned commit failed for %s: %s",
                    _strip_git_target_userinfo(git_url),
                    str(err),
                )
                return None
        elif isolate_transport:
            # A credentialed `git clone` performs both the network fetch and the
            # checkout in one process. The checkout may launch an inherited filter
            # selected by the fetched `.gitattributes`, which would inherit the
            # one-shot URL rewrite. Split the operations and give only fetch the
            # credential-bearing environment.
            fetch_log = []
            fetch_dest = Path(tmp_root) / "branch"
            checkout_root = fetch_dest
            err = await _git_fetch_branch(
                git_url,
                branch,
                fetch_dest,
                fetch_log,
                credential_target=credential_target,
                clone_env=clone_env,
                sandbox_mode=sandbox_mode,
            )
            if err is not None:
                logger.debug(
                    "manifest fetch of branch failed for %s: %s",
                    _strip_git_target_userinfo(git_url),
                    str(err),
                )
                return None
        else:
            clone_cmd = [
                "git",
                *_HOOKS_NEUTRALIZER_ARGV,
                "clone",
                "--depth",
                "1",
                "--branch",
                branch,
                "--single-branch",
                git_url,
                tmp_root,
            ]
            sandboxed_cmd, _cleanup = await wrap_argv_async(
                clone_cmd,
                mode=sandbox_mode,
                forward_ssh_auth_sock=owner_designated,
                _prepare=wrap_argv,
            )
            sandboxed_cmd = cgroup_scope_argv(sandboxed_cmd)  # cgroup DoS ceiling
            transport_env = _git_network_env(
                _git_transport_env(credential_target, git_url, clone_env), git_url
            )
            proc = await create_subprocess_limited(
                *sandboxed_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=transport_env,
                start_new_session=platform_compat.IS_POSIX,
                creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            )
            _, stderr = await _communicate_with_timeout(proc, timeout=_CLONE_TIMEOUT)
            if proc.returncode != 0:
                stderr_text = stderr.decode(errors="replace")
                if outcome is not None and _is_ssh_sign_in_refusal(stderr_text):
                    outcome["ssh_sign_in"] = "refused"
                logger.debug(
                    "manifest clone failed for %s: %s",
                    _strip_git_target_userinfo(git_url),
                    _loggable_git_transport_output(
                        stderr_text.strip(),
                        credentialed=credentialed_transport,
                    ),
                )
                return None
            checkout_root = Path(tmp_root)
        # Containment is measured from the root the tree actually landed in, which
        # differs by branch: the pinned fetch uses a child of `tmp_root` so it gets a
        # destination it created, the branch clone uses `tmp_root` itself.
        manifest_dir = _contained_join(checkout_root, subdirectory)
        if manifest_dir is None:
            # Untrusted index subdirectory escaped the clone root (absolute,
            # ``..``, or a symlink resolving outside tmp_root) — refuse.
            return None
        content = await asyncio.to_thread(_read_manifest_text, manifest_dir / "app.json")
        if content is None:
            return None
        data = loads_user_json(content)
        # A manifest that is not a JSON object (``["x"]``, ``"str"``) is no manifest.
        return data if isinstance(data, dict) else None
    except (asyncio.TimeoutError, OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.debug(
            "Failed to fetch app.json from %s: %s",
            _strip_git_target_userinfo(git_url),
            _loggable_git_transport_output(str(exc), credentialed=credentialed_transport),
        )
        return None
    finally:
        if tmp_root:
            await _rmtree_force_settled(tmp_root)


#: How long the listing stops asking an ssh destination after it refused an
#: anonymous sign-in. The same window as a row's failed-fetch marker, which every
#: skipped row also writes, so both lapse together and the next listing asks the
#: destination once.
_SSH_SIGN_IN_REFUSED_TTL = _MANIFEST_NEGATIVE_CACHE_TTL

#: Most ssh destinations remembered at once, across the refused, answered and
#: probe stores together (``_ssh_sign_in_admits``). An index names its own clone
#: URLs, so one listing several thousand ``user@host`` spellings would otherwise
#: grow these without bound. At the cap the settled results are forgotten and
#: asked again; a destination that finds the cap full of in-flight probes gets no
#: row anywhere and is fetched as before.
_SSH_SIGN_IN_MEMORY_MAX = 1024

#: Longest ssh destination remembered. A host name holds at most 253 characters,
#: which leaves room for a user name and a port; a longer spelling, which only an
#: index row can write, is fetched as before and never remembered.
_SSH_DESTINATION_MAX_CHARS = 320

#: Longest a row waits for its destination's probe before it fetches anyway. ssh
#: refuses a sign-in within a second or two. A host that drops the connection
#: instead holds the probe for its whole clone timeout, and without this bound
#: every other row would wait that long first and then pay the timeout again.
_SSH_SIGN_IN_PROBE_WAIT = 5.0

#: Process memory, never disk, of how anonymous ssh sign-ins went, keyed by ssh
#: destination (``_anonymous_ssh_destination``):
#:   refused   -> monotonic time ssh last answered ``Permission denied (...)``;
#:   answered  -> a remote fetch ran without a refusal, so rows fan out;
#:   probes    -> the one in-flight fetch the other rows on that destination await.
#: The data home is agent-writable, so a marker there could hold fetches back;
#: memory cannot be planted and is rebuilt by one probe after a restart.
_SSH_SIGN_IN_REFUSED: dict[str, float] = {}
_SSH_SIGN_IN_ANSWERED: set[str] = set()
_SSH_SIGN_IN_PROBES: dict[str, asyncio.Future[None]] = {}

#: Destinations turned away since the memory last admitted one, so a full memory
#: is logged once, not once per row.
_SSH_SIGN_IN_OVERFLOW: dict[str, int] = {"turned_away": 0}


def _is_ssh_sign_in_refusal(stderr_text: str) -> bool:
    """Whether *stderr_text* holds ssh's own sign-in refusal as a whole line.

    OpenSSH writes ``<user>@<host>: Permission denied (<methods>).`` on a line of
    its own; an old client leaves out the ``<user>@<host>: ``. The phrase found
    anywhere is not enough, because git echoes the repository path in its own
    errors (``fatal: '<path>' does not appear to be a git repository``) and an
    index chooses that path: one crafted row would mark every app at its
    destination as refused. So the line must be that optional ``user@host: ``,
    holding no space, quote or parenthesis, then ``Permission denied (``, a method
    list (``_ssh_method_list_closes``), and nothing after it but one ``.``. Each
    line is read a bounded number of times.
    """
    for line in stderr_text.lower().splitlines():
        line = line.strip()
        if not line.startswith(_SSH_REFUSAL_PREFIX):
            who, sep, line = line.partition(": ")
            if not sep or "@" not in who or any(ch.isspace() or ch in "'\"()" for ch in who):
                continue
            if not line.startswith(_SSH_REFUSAL_PREFIX):
                continue
        methods = line[len(_SSH_REFUSAL_PREFIX) :]
        close = methods.find(")")
        if close > 0 and methods[close + 1 :] in ("", ".") and _ssh_method_list_closes(methods, 0):
            return True
    return False


def _anonymous_ssh_destination(git_url: str) -> str:
    """The ssh destination an anonymous fetch of *git_url* signs in to, or ``""``.

    The ``user@host[:port]`` authority of an ``ssh://`` URL, or the ``user@host``
    of an scp-style target, as written. Any other transport answers ``""``: an
    https fetch is refused per repository (a public and a private repository share
    a host), while ssh refuses the sign-in before a repository is named. The user
    is part of the key, so an index row naming another user at the same host
    affects only rows that name that user. A destination longer than
    ``_SSH_DESTINATION_MAX_CHARS`` also answers ``""``, and so does a URL holding a
    percent escape, a space, a control or a non-ASCII character: git decodes the
    path and echoes it in its errors, so such a URL could print a line that reads
    as ssh's refusal (``_is_ssh_sign_in_refusal``).
    """
    target = _strip_git_target_userinfo(git_url or "").strip()
    if not _is_ssh_git_url(target) or not _git_url_host(target):
        return ""
    if (
        "%" in target
        or not target.isascii()
        or not target.isprintable()
        or any(ch.isspace() for ch in target)
    ):
        return ""
    scheme_end = target.find("://")
    if scheme_end >= 0:
        destination = target[scheme_end + 3 :].split("/", 1)[0]
    else:
        user, at, rest = target.rpartition("@")
        # A bracketed IPv6 literal holds colons of its own; the path follows "]:".
        host = rest[: rest.find("]") + 1] if rest.startswith("[") else rest.split(":", 1)[0]
        destination = f"{user}{at}{host}"
    return destination if len(destination) <= _SSH_DESTINATION_MAX_CHARS else ""


def _forget_ssh_sign_in_memory() -> None:
    """Drop every remembered sign-in result (an explicit refresh, and tests)."""
    _SSH_SIGN_IN_REFUSED.clear()
    _SSH_SIGN_IN_ANSWERED.clear()
    _SSH_SIGN_IN_OVERFLOW["turned_away"] = 0


def _ssh_sign_in_admits(destination: str) -> bool:
    """Whether *destination* may hold a row in the sign-in memory.

    The one admission check for the refused, answered and probe stores, which
    ``_SSH_SIGN_IN_MEMORY_MAX`` bounds together. A destination already held is
    admitted. At the cap the settled results are forgotten, to be asked again.
    A new destination is turned away only while in-flight probes alone fill the
    cap: it gets no row in any store, and its fetch runs uncoordinated, as it did
    before this memory. The first destination turned away is logged with the
    probe count; the next log comes after the memory has admitted one again.
    """
    if (
        destination in _SSH_SIGN_IN_REFUSED
        or destination in _SSH_SIGN_IN_ANSWERED
        or destination in _SSH_SIGN_IN_PROBES
    ):
        return True
    held = len(_SSH_SIGN_IN_REFUSED) + len(_SSH_SIGN_IN_ANSWERED) + len(_SSH_SIGN_IN_PROBES)
    if held >= _SSH_SIGN_IN_MEMORY_MAX:
        _SSH_SIGN_IN_REFUSED.clear()
        _SSH_SIGN_IN_ANSWERED.clear()
    if len(_SSH_SIGN_IN_PROBES) < _SSH_SIGN_IN_MEMORY_MAX:
        _SSH_SIGN_IN_OVERFLOW["turned_away"] = 0
        return True
    _SSH_SIGN_IN_OVERFLOW["turned_away"] += 1
    if _SSH_SIGN_IN_OVERFLOW["turned_away"] == 1:
        logger.warning(
            "the store listing's ssh sign-in memory is full with %d in-flight probes; "
            "further ssh destinations are fetched without it until a probe ends",
            len(_SSH_SIGN_IN_PROBES),
        )
    return False


async def _ssh_sign_in_refused(destination: str) -> tuple[bool, asyncio.Future[None] | None]:
    """``(refused, probe)`` for a listing fetch at *destination*.

    Awaited right before the remote step, so only a row that is about to sign in
    waits here. ``refused`` is True while a refusal is fresh: the caller skips the
    remote. Otherwise, when no fetch there has run yet, the caller becomes the
    probe and gets the future it must release when its fetch ends; every other
    caller waits for that release, up to ``_SSH_SIGN_IN_PROBE_WAIT``, and reads
    the result. A waiter that runs out of patience fetches without the probe's
    answer. A probe that learned nothing leaves no result, so the next waiter
    probes in turn. A destination ``_ssh_sign_in_admits`` turns away gets
    ``(False, None)`` and no probe.
    """
    loop = asyncio.get_running_loop()
    while True:
        refused_at = _SSH_SIGN_IN_REFUSED.get(destination)
        if refused_at is not None:
            if 0 <= time.monotonic() - refused_at <= _SSH_SIGN_IN_REFUSED_TTL:
                return True, None
            _SSH_SIGN_IN_REFUSED.pop(destination, None)
        if destination in _SSH_SIGN_IN_ANSWERED:
            return False, None
        pending = _SSH_SIGN_IN_PROBES.get(destination)
        if pending is None or pending.done() or pending.get_loop() is not loop:
            if not _ssh_sign_in_admits(destination):
                return False, None
            probe = loop.create_future()
            _SSH_SIGN_IN_PROBES[destination] = probe
            return False, probe
        # Shielded: a waiter cancelled or timed out here must not cancel the
        # shared future.
        try:
            await asyncio.wait_for(asyncio.shield(pending), _SSH_SIGN_IN_PROBE_WAIT)
        except TimeoutError:
            return False, None


def _note_ssh_sign_in(destination: str, result: str | None) -> None:
    """Record what a fetch at *destination* learned: ``refused``, ``attempted`` or nothing.

    A destination ``_ssh_sign_in_admits`` turns away is not recorded.
    """
    if result is None or not _ssh_sign_in_admits(destination):
        return
    if result == "refused":
        if destination not in _SSH_SIGN_IN_REFUSED:
            logger.info(
                "ssh refused the anonymous sign-in at %s; its other store listing "
                "fetches are skipped for %d s (Refresh asks again)",
                _git_url_host(f"ssh://{destination}/") or "an ssh host",
                _SSH_SIGN_IN_REFUSED_TTL,
            )
        _SSH_SIGN_IN_REFUSED[destination] = time.monotonic()
        _SSH_SIGN_IN_ANSWERED.discard(destination)
    else:
        _SSH_SIGN_IN_ANSWERED.add(destination)


def _release_ssh_probe(destination: str, probe: asyncio.Future[None]) -> None:
    """End *probe* so the rows waiting on *destination* read its result."""
    if _SSH_SIGN_IN_PROBES.get(destination) is probe:
        _SSH_SIGN_IN_PROBES.pop(destination, None)
    if not probe.done():
        probe.set_result(None)


async def _resolve_manifest(entry: dict[str, Any]) -> dict[str, Any]:
    """Merge registry entry with its remote app.json manifest.

    Returns the entry enriched with display fields from app.json.
    Registry fields (name, repo, branch, managed, detectInstalled) take
    precedence; everything else comes from app.json.
    """
    name = entry.get("name", "")
    repo = entry.get("repo", "")
    branch = entry.get("branch", "main")
    subdirectory = entry.get("subdirectory", "")
    git_url = _entry_git_url(entry)

    if not git_url:
        return entry

    # Try cache first — keyed on the entry's full source coordinates, so a
    # row configured for another branch/repo/subdirectory can never answer.
    cached = await asyncio.to_thread(_read_manifest_cache, entry)
    if cached:
        return _merge_manifest(entry, cached)
    # Then the installed checkout's copy, cached apart (see the write below).
    installed = await asyncio.to_thread(_read_installed_manifest_cache, entry)
    if installed:
        return _merge_manifest(entry, installed)

    # The same switch that keeps the catalog documents off the network keeps
    # the listing's clone off it too: a seed row with no cached manifest
    # renders its minimal info, which is exactly the failed-fetch degradation
    # below. The install path (``install.py``) calls ``_fetch_app_manifest``
    # directly and is not gated here -- an install needs the network anyway.
    if official_catalog.store_fetch_skipped():
        logger.info("%s=1 — not fetching app.json for %s", official_catalog.SKIP_FETCH_ENV, name)
        return entry

    # A fetch for these exact source coordinates failed within
    # ``_MANIFEST_NEGATIVE_CACHE_TTL``: skip the remote clone. The local
    # installed clone is still read first, so an app installed since the
    # failure keeps its manifest. The marker shares the success cache's key,
    # so another branch, repository or subdirectory still fetches. Only the
    # listing consults it; the install path calls ``_fetch_app_manifest``
    # directly.
    negative_cached = await asyncio.to_thread(_read_manifest_negative_cache, entry)

    # Fetch from repo
    # Same-repo credential carve-out: if the entry's clone URL matches the
    # owner-configured registry repo, use owner credentials for the manifest
    # fetch (the confused-deputy defense does not apply to the owner's own URL).
    owner_target = await asyncio.to_thread(_owner_designated_repo_target, entry)
    fetch_kwargs: dict[str, Any] = {"allow_remote": False} if negative_cached else {}

    # An anonymous ssh fetch whose destination just refused the sign-in cannot
    # succeed: the posture offers no key and no agent, and the sign-in does not
    # depend on the repository. Such a row skips the remote after its local
    # installed copy is read, and then fails as a fetch would, writing the same
    # marker. Right before the remote step, one fetch per destination probes it
    # while the other rows about to sign in there wait, so a cold listing pays one
    # refusal per destination instead of one per app. A row answered from its
    # installed copy never waits. An owner-designated fetch carries the owner's
    # credentials and is never skipped here.
    destination = "" if owner_target or negative_cached else _anonymous_ssh_destination(git_url)
    probes: list[asyncio.Future[None]] = []

    async def _sign_in_not_refused() -> bool:
        refused, probe = await _ssh_sign_in_refused(destination)
        if probe is not None:
            probes.append(probe)
        return not refused

    if destination:
        fetch_kwargs["before_remote"] = _sign_in_not_refused
    provenance: dict[str, str] = {}
    outcome: dict[str, str] = {}
    try:
        manifest = await _fetch_app_manifest(
            repo,
            branch,
            subdirectory,
            app_name=name,
            git_url=owner_target or git_url,
            owner_designated=bool(owner_target),
            provenance=provenance,
            outcome=outcome,
            **fetch_kwargs,
        )
    finally:
        if destination:
            _note_ssh_sign_in(destination, outcome.get("ssh_sign_in"))
        for probe in probes:
            _release_ssh_probe(destination, probe)
    if manifest:
        # The installed checkout's app.json is cached APART from this source's
        # manifest. It names the INSTALLED version, while the source cache is what
        # the listing's ``version``, the update status and the prewarm's warm check
        # read as what the source publishes: stored there, it made an installed app
        # read as current for the cache's life, and kept the owner-tier prewarm, the
        # only credentialed reader of the branch tip, from renewing it. The source
        # cache is read first above, so a tip the prewarm fetched wins.
        writer = (
            _write_installed_manifest_cache
            if provenance.get("source") == "installed"
            else _write_manifest_cache
        )
        await asyncio.to_thread(writer, entry, manifest)
        return _merge_manifest(entry, manifest)

    if negative_cached:
        # The remote was skipped, so nothing new failed: leave the marker's
        # age alone so it still lapses on time.
        return entry

    # No manifest available — return entry as-is (minimal info). The failed
    # fetch attaches NOTHING: the source-scoped cache read above already
    # missed, and there is deliberately no name-only fallback that could
    # attach a manifest cached for another branch or repository.
    # The negative-cache marker stops the next listings from cloning again.
    await asyncio.to_thread(_write_manifest_negative_cache, entry)
    logger.info("Could not fetch app.json for %s — showing minimal info", name)
    return entry


#: Keys a registry index row may contribute to a merged app-store row.
#
# An index row is UNTRUSTED content — an external registry's index is
# user-supplied JSON — so the merge projects these names explicitly instead of
# spreading the row. Spreading shipped every key an index chose to invent
# straight to the browser, which both grew the payload with fields no consumer
# reads and gave an index a channel for keys the client never validated.
#
# Each name here has a reader: ``name`` is identity; ``gitUrl`` / ``repo`` /
# ``branch`` / ``subdirectory`` are the clone coordinates
# (``_entry_git_url``, ``install_from_registry``); ``resources`` selects the
# self-managed install path; ``detectInstalled`` is the pre-install probe;
# ``managed`` is the legacy registry-only flag; ``featured`` is the Discover
# spotlight flag (kept only on non-external rows by ``_apply_trust_fields``);
# ``_registry`` is the server-attached source tag; ``_index_author`` is the
# author snapshot ``_apply_trust_fields`` consumes for the verified mark.
#
# Display fields are deliberately ABSENT: they come from the fetched
# ``app.json`` below, so an index cannot publish display copy for an app whose
# manifest says otherwise. The one exception is ``version`` on an external row,
# which the index may RAISE after this merge (``_append_external_registry_apps``)
# because an installed app's manifest is read from its own installed checkout.
# Install-status and trust fields are also absent —
# ``_enrich_with_install_status`` and ``_apply_trust_fields`` run after this
# and stamp them server-side.

_REGISTRY_ROW_KEYS: frozenset[str] = frozenset(
    {
        "name",
        "gitUrl",
        "repo",
        "branch",
        # `_catalog` must survive the merge or the row goes back through the
        # per-app manifest clone it exists to make unnecessary.
        #
        # `commit` survives for data fidelity only, NOT as an authorization: this
        # projection also builds rows from an external registry's index, so the
        # value here is index-controlled. `install_from_registry` reads the pin only
        # for `_is_catalog_row`, which no index row can satisfy.
        "commit",
        "_catalog",
        "subdirectory",
        "resources",
        "detectInstalled",
        "managed",
        "featured",
        # GitHub star count baked into the row by the publisher (git-type
        # third-party apps only). Reader: the frontend App Store list/detail
        # display. Display-only — ``_apply_trust_fields`` sanitizes it to a
        # non-negative int on EVERY row (the allowlist is not the only exit:
        # a failed manifest fetch passes the row through unchanged).
        "stargazersCount",
        "_registry",
        "_index_author",
    }
)


def _merge_manifest(entry: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    """Merge app.json fields into a registry entry.

    Registry-only fields (``_REGISTRY_ROW_KEYS``) are preserved from the entry.
    Everything else comes from app.json, with the blob proxy URL pattern
    applied to image paths -- each joined under the entry's ``subdirectory``
    first (:func:`_store_asset_path`), the directory the manifest was read from.
    """
    raw_repo = entry.get("repo", "")
    repo = _strip_git_target_userinfo(raw_repo) if isinstance(raw_repo, str) else ""
    result = {k: v for k, v in entry.items() if k in _REGISTRY_ROW_KEYS}
    if isinstance(result.get("repo"), str):
        result["repo"] = _strip_git_target_userinfo(result["repo"])
    subdirectory = entry.get("subdirectory", "")

    def _blob_url(asset_path: str) -> str:
        return f"/api/apps/blob?repo={repo}&path={_store_asset_path(subdirectory, asset_path)}"

    # Top-level display fields from app.json
    for key in (
        "displayName",
        "description",
        "version",
        "author",
        "tags",
        "highlights",
        "useCases",
        "configuration",
        "license",
        "minKiroCrewVersion",
    ):
        if key in manifest:
            result[key] = manifest[key]

    # Runtime fields go under "manifest" — matches the installed app
    # data structure so the frontend can always read app.manifest.*
    manifest_fields: dict[str, Any] = {}
    for key in (
        "agents",
        "skills",
        "crons",
        "mcpServers",
        "permissions",
        "setup",
        "ui",
        "openCommand",
    ):
        if key in manifest:
            manifest_fields[key] = manifest[key]
    if manifest_fields:
        result["manifest"] = manifest_fields

    # Platform config from app.json
    if "platform" in manifest:
        result["platform"] = manifest["platform"]

    # Store-page links, checked here because this manifest is untrusted content.
    if "links" in manifest:
        links = sanitize_links(manifest["links"], app_name=str(entry.get("name", "")))
        if links:
            result["links"] = links

    # Icon — convert a manifest-relative path to a blob proxy URL, joined under
    # the entry's ``subdirectory`` (the directory app.json was read from) so the
    # blob path names the file where it actually lives in the repo.
    #
    # Only ``iconPath`` (repo-relative) is honoured, never a manifest-declared
    # ``iconUrl``: an index-fetched manifest is untrusted content, and copying an
    # absolute URL out of it would let a third party point the store's <img> at
    # any host it likes. Rewriting a repo-relative path keeps every icon fetch
    # on our own proxy, which enforces the extension allowlist and the
    # trusted-host gate.
    icon_path = manifest.get("iconPath", "")
    if icon_path and repo:
        result["iconUrl"] = _blob_url(icon_path)
    # Dark-appearance variant. Raster icons have fixed bytes, so an app that
    # must read well on both backgrounds ships two files; first-party
    # ``/app-assets/`` SVGs are inlined and repaint from theme tokens instead.
    icon_path_dark = manifest.get("iconPathDark", "")
    if icon_path_dark and repo:
        result["iconUrlDark"] = _blob_url(icon_path_dark)
    # Lucide fallback icon from manifest extra fields
    if manifest.get("icon"):
        result["icon"] = manifest["icon"]

    # Screenshots — convert repo-relative paths to blob proxy URLs
    screenshots = manifest.get("screenshots", [])
    if screenshots and repo:
        result["screenshots"] = [_blob_url(p) for p in screenshots]

    # Screenshots dark — convert repo-relative paths to blob proxy URLs
    screenshots_dark = manifest.get("screenshotsDark", [])
    if screenshots_dark and repo:
        result["screenshotsDark"] = [_blob_url(p) for p in screenshots_dark]

    # Hero images — convert repo-relative paths to blob proxy URLs
    hero = manifest.get("heroImage", "")
    if hero and repo:
        result["heroImage"] = _blob_url(hero)
    hero_dark = manifest.get("heroImageDark", "")
    if hero_dark and repo:
        result["heroImageDark"] = _blob_url(hero_dark)
    # Detail-page hero images (wide banner ratio) — convert repo-relative paths
    # to blob proxy URLs. The detail page prefers these over the (near-square)
    # Browse-card hero so the wide banner isn't cropped.
    hero_detail = manifest.get("heroImageDetail", "")
    if hero_detail and repo:
        result["heroImageDetail"] = _blob_url(hero_detail)
    hero_detail_dark = manifest.get("heroImageDetailDark", "")
    if hero_detail_dark and repo:
        result["heroImageDetailDark"] = _blob_url(hero_detail_dark)

    return result
